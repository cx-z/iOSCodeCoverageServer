#!/usr/bin/env python3
"""Strict llvm-cov export path and code-region normalization for Git lines."""

import os
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Dict, List, Mapping, Optional, Sequence, Set, Tuple


SUPPORTED_EXPORT_VERSIONS = {"2.0.1"}
CODE_REGION_KIND = 0
SKIPPED_REGION_KIND = 2
GAP_REGION_KIND = 3


class CoverageExportError(Exception):
    """Describes an unsupported coverage export envelope or global corruption."""

    def __init__(self, code: str, message: str):
        """Creates one stable coverage normalization error."""

        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True, order=True)
class CoverageRegion:
    """Represents one compiler code region with an execution count."""

    start_line: int
    start_column: int
    end_line: int
    end_column: int
    count: int
    function_name: str

    def as_dict(self) -> Dict[str, object]:
        """Serializes one region without inferring execution frequency semantics."""

        return {
            "start_line": self.start_line,
            "start_column": self.start_column,
            "end_line": self.end_line,
            "end_column": self.end_column,
            "count": self.count,
            "has_count": True,
            "is_gap_region": False,
            "function_name": self.function_name,
        }


@dataclass(frozen=True)
class NormalizedCoverage:
    """Holds exact repository mappings and per-path reliability failures."""

    export_version: str
    regions_by_path: Mapping[str, Tuple[CoverageRegion, ...]]
    conflicted_paths: Mapping[str, str]
    unmapped_files: Tuple[str, ...]

    def document(self) -> Dict[str, object]:
        """Creates deterministic evidence suitable for hashing and review."""

        all_paths = sorted(set(self.regions_by_path) | set(self.conflicted_paths))
        return {
            "schema_version": 1,
            "llvm_cov_export_format_version": self.export_version,
            "files": [
                {
                    "repo_relative_path": path,
                    "status": "unknown" if path in self.conflicted_paths else "mapped",
                    "reason": self.conflicted_paths.get(path),
                    "measurable_regions": [
                        region.as_dict() for region in self.regions_by_path.get(path, ())
                    ],
                }
                for path in all_paths
            ],
            "unmapped_coverage_files": list(self.unmapped_files),
        }


def normalize_coverage_export(
    payload: Mapping[str, object], repository_path: Path
) -> NormalizedCoverage:
    """Maps only proven repository-absolute filenames and code regions."""

    if payload.get("type") != "llvm.coverage.json.export":
        raise CoverageExportError("coverage_export_unsupported", "unsupported llvm-cov export type")
    version = payload.get("version")
    if version not in SUPPORTED_EXPORT_VERSIONS:
        raise CoverageExportError(
            "coverage_export_unsupported", "unsupported llvm-cov export format version"
        )
    data = payload.get("data")
    if not isinstance(data, list) or len(data) != 1 or not isinstance(data[0], dict):
        raise CoverageExportError("coverage_export_invalid", "llvm-cov export data is invalid")
    export_data = data[0]
    files = export_data.get("files")
    functions = export_data.get("functions")
    if not isinstance(files, list) or not isinstance(functions, list):
        raise CoverageExportError(
            "coverage_export_invalid", "llvm-cov export files/functions are missing"
        )

    root = repository_path.resolve()
    file_segments: Dict[str, object] = {}
    conflicts: Dict[str, str] = {}
    unmapped: Set[str] = set()
    for file_record in files:
        if not isinstance(file_record, dict) or not isinstance(file_record.get("filename"), str):
            raise CoverageExportError("coverage_export_invalid", "coverage file record is invalid")
        filename = str(file_record["filename"])
        relative = _coverage_repo_path(filename, root)
        if relative is None:
            unmapped.add(filename)
            continue
        segments = file_record.get("segments")
        if not isinstance(segments, list):
            conflicts[relative] = "coverage_segments_invalid"
            continue
        if relative in file_segments and file_segments[relative] != segments:
            conflicts[relative] = "conflicting_coverage_file_records"
        else:
            file_segments[relative] = segments

    regions: Dict[str, Set[CoverageRegion]] = {path: set() for path in file_segments}
    for function in functions:
        if not isinstance(function, dict):
            raise CoverageExportError("coverage_export_invalid", "coverage function is invalid")
        filenames = function.get("filenames")
        raw_regions = function.get("regions")
        function_name = function.get("name")
        if (
            not isinstance(filenames, list)
            or not all(isinstance(value, str) for value in filenames)
            or not isinstance(raw_regions, list)
            or not isinstance(function_name, str)
        ):
            raise CoverageExportError("coverage_export_invalid", "coverage function fields are invalid")
        for raw_region in raw_regions:
            if not isinstance(raw_region, list) or len(raw_region) != 8:
                raise CoverageExportError("coverage_export_invalid", "coverage region shape is invalid")
            if not all(isinstance(value, int) and not isinstance(value, bool) for value in raw_region):
                raise CoverageExportError("coverage_export_invalid", "coverage region values are invalid")
            start_line, start_column, end_line, end_column, count, file_id, _, kind = raw_region
            if file_id < 0 or file_id >= len(filenames):
                raise CoverageExportError("coverage_export_invalid", "coverage region file index is invalid")
            filename = filenames[file_id]
            relative = _coverage_repo_path(filename, root)
            if relative is None:
                unmapped.add(filename)
                continue
            if relative not in file_segments:
                conflicts[relative] = "coverage_function_has_no_file_record"
                continue
            if kind in (SKIPPED_REGION_KIND, GAP_REGION_KIND):
                continue
            if kind != CODE_REGION_KIND:
                conflicts[relative] = "unsupported_coverage_region_kind"
                continue
            if (
                start_line <= 0
                or start_column <= 0
                or end_line <= 0
                or end_column <= 0
                or count < 0
                or (end_line, end_column) < (start_line, start_column)
            ):
                conflicts[relative] = "coverage_region_position_invalid"
                continue
            if (end_line, end_column) == (start_line, start_column):
                # LLVM 2.0.1 may emit zero-length code regions. They carry no
                # source area, so they are valid input but cannot measure a line.
                continue
            regions.setdefault(relative, set()).add(
                CoverageRegion(
                    start_line,
                    start_column,
                    end_line,
                    end_column,
                    count,
                    function_name,
                )
            )

    return NormalizedCoverage(
        export_version=str(version),
        regions_by_path={
            path: tuple(sorted(values)) for path, values in sorted(regions.items())
        },
        conflicted_paths=dict(sorted(conflicts.items())),
        unmapped_files=tuple(sorted(unmapped)),
    )


def regions_for_source_line(
    regions: Sequence[CoverageRegion], line_number: int, source_lines: Sequence[str]
) -> Tuple[Tuple[CoverageRegion, ...], Optional[str]]:
    """Returns code regions intersecting one source line or a reliability reason."""

    if line_number <= 0 or line_number > len(source_lines):
        return (), "git_changed_line_out_of_range"
    related: List[CoverageRegion] = []
    for region in regions:
        if region.start_line > len(source_lines) or region.end_line > len(source_lines):
            return (), "coverage_region_out_of_range"
        start_max = len(source_lines[region.start_line - 1].encode("utf-8")) + 1
        end_max = len(source_lines[region.end_line - 1].encode("utf-8")) + 1
        if region.start_column > start_max or region.end_column > end_max:
            return (), "coverage_region_column_out_of_range"
        if _region_intersects_line(region, line_number, source_lines[line_number - 1]):
            related.append(region)
    return tuple(related), None


def _region_intersects_line(
    region: CoverageRegion, line_number: int, source_line: str
) -> bool:
    """Intersects LLVM's half-open source range with one concrete source line."""

    if line_number < region.start_line or line_number > region.end_line:
        return False
    line_end = len(source_line.encode("utf-8")) + 1
    if region.start_line == region.end_line == line_number:
        return region.start_column < region.end_column and region.start_column <= line_end
    if line_number == region.start_line:
        return region.start_column <= line_end
    if line_number == region.end_line:
        return region.end_column > 1
    return True


def _coverage_repo_path(filename: str, repository_root: Path) -> Optional[str]:
    """Strictly maps one normalized absolute compiler path under the fixed root."""

    if not filename or "\x00" in filename or not os.path.isabs(filename):
        return None
    normalized = os.path.normpath(filename)
    if normalized != filename:
        return None
    path = Path(filename).resolve()
    try:
        relative = path.relative_to(repository_root)
    except ValueError:
        return None
    value = PurePosixPath(relative.as_posix()).as_posix()
    if not value or value == "." or ".." in PurePosixPath(value).parts:
        return None
    return value
