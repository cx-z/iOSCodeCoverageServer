#!/usr/bin/env python3
"""Conservative Git-proven source-region mapping across immutable Build commits."""

import hashlib
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Dict, Mapping, Optional, Sequence, Tuple

from ioscoverage.analysis.mapping.coverage_export import CoverageRegion, NormalizedCoverage
from ioscoverage.analysis.diff.git_diff_parser import NormalizedDiffFile


@dataclass(frozen=True)
class FileLineMapping:
    """Maps unchanged current lines back to one historical Git object file."""

    historical_path: Optional[str]
    current_path: str
    git_status: str
    current_to_historical_lines: Mapping[int, int]
    reason: str


@dataclass(frozen=True)
class RegionInheritance:
    """Describes whether one current zero-count region inherits a historical hit."""

    inherited: bool
    reason: str
    evidence: Mapping[str, object]


class SourceRegionMappingError(Exception):
    """Describes source or hunk data that cannot be interpreted reliably."""

    def __init__(self, code: str, message: str):
        """Creates one stable mapping error."""

        super().__init__(message)
        self.code = code
        self.message = message


def select_file_diff(
    current_path: str, files: Sequence[NormalizedDiffFile]
) -> Optional[NormalizedDiffFile]:
    """Finds the unique Git entry whose new side is the current path."""

    matches = [item for item in files if item.new_path == current_path]
    if len(matches) > 1:
        raise SourceRegionMappingError(
            "ambiguous_file_mapping", "multiple Git entries map to one current path"
        )
    return matches[0] if matches else None


def build_file_line_mapping(
    current_path: str,
    historical_source: Optional[str],
    current_source: str,
    diff_file: Optional[NormalizedDiffFile],
) -> FileLineMapping:
    """Builds an exact unchanged-line map from zero-context Git hunks."""

    if diff_file is not None and diff_file.status == "A":
        return FileLineMapping(None, current_path, "A", {}, "file_added_after_historical_build")
    historical_path = diff_file.old_path if diff_file is not None else current_path
    if historical_path is None or historical_source is None:
        return FileLineMapping(
            historical_path,
            current_path,
            diff_file.status if diff_file is not None else "missing",
            {},
            "historical_file_missing",
        )
    if diff_file is not None and diff_file.binary:
        return FileLineMapping(
            historical_path, current_path, diff_file.status, {}, "binary_file_not_mappable"
        )

    old_lines = historical_source.splitlines(keepends=True)
    new_lines = current_source.splitlines(keepends=True)
    mapping: Dict[int, int] = {}
    old_cursor = 1
    new_cursor = 1
    hunks = diff_file.hunks if diff_file is not None else ()
    for hunk in hunks:
        old_first = hunk.old_start if hunk.old_count else hunk.old_start + 1
        new_first = hunk.new_start if hunk.new_count else hunk.new_start + 1
        old_unchanged = old_first - old_cursor
        new_unchanged = new_first - new_cursor
        if old_unchanged < 0 or old_unchanged != new_unchanged:
            raise SourceRegionMappingError(
                "ambiguous_line_mapping", "Git hunk offsets cannot produce a unique line map"
            )
        _map_equal_block(old_lines, new_lines, old_cursor, new_cursor, old_unchanged, mapping)
        old_cursor = old_first + hunk.old_count
        new_cursor = new_first + hunk.new_count
    old_remaining = len(old_lines) - old_cursor + 1
    new_remaining = len(new_lines) - new_cursor + 1
    if old_remaining < 0 or old_remaining != new_remaining:
        raise SourceRegionMappingError(
            "ambiguous_line_mapping", "Git trailing ranges cannot produce a unique line map"
        )
    _map_equal_block(old_lines, new_lines, old_cursor, new_cursor, old_remaining, mapping)
    status = diff_file.status if diff_file is not None else "unchanged"
    reason = "git_rename_and_unchanged_hunks" if status == "R" else "git_unchanged_hunks"
    return FileLineMapping(historical_path, current_path, status, mapping, reason)


def map_region_to_history(
    current_path: str,
    current_region: CoverageRegion,
    current_source: str,
    historical_source: str,
    file_mapping: FileLineMapping,
    historical_coverage: NormalizedCoverage,
) -> RegionInheritance:
    """Requires exact Git line mapping, source bytes, function, and region range."""

    base_evidence: Dict[str, object] = {
        "current_path": current_path,
        "historical_path": file_mapping.historical_path,
        "git_status": file_mapping.git_status,
        "file_mapping_reason": file_mapping.reason,
        "current_region": current_region.as_dict(),
    }
    if file_mapping.historical_path is None:
        return RegionInheritance(False, file_mapping.reason, base_evidence)
    historical_path = file_mapping.historical_path
    if historical_path in historical_coverage.conflicted_paths:
        base_evidence["historical_coverage_conflict"] = historical_coverage.conflicted_paths[
            historical_path
        ]
        return RegionInheritance(False, "historical_coverage_conflict", base_evidence)

    current_lines = range(current_region.start_line, current_region.end_line + 1)
    mapped_lines = [file_mapping.current_to_historical_lines.get(line) for line in current_lines]
    if any(line is None for line in mapped_lines):
        return RegionInheritance(False, "region_contains_modified_or_added_line", base_evidence)
    historical_lines = [int(line) for line in mapped_lines if line is not None]
    if any(right != left + 1 for left, right in zip(historical_lines, historical_lines[1:])):
        return RegionInheritance(False, "region_line_mapping_not_contiguous", base_evidence)

    historical_start = historical_lines[0]
    historical_end = historical_lines[-1]
    current_snippet = source_region_bytes(current_source, current_region)
    historical_shape = CoverageRegion(
        historical_start,
        current_region.start_column,
        historical_end,
        current_region.end_column,
        0,
        current_region.function_name,
    )
    historical_snippet = source_region_bytes(historical_source, historical_shape)
    current_sha = hashlib.sha256(current_snippet).hexdigest()
    historical_sha = hashlib.sha256(historical_snippet).hexdigest()
    base_evidence.update(
        {
            "current_source_range_sha256": current_sha,
            "historical_source_range_sha256": historical_sha,
            "historical_region_range": {
                "start_line": historical_start,
                "start_column": current_region.start_column,
                "end_line": historical_end,
                "end_column": current_region.end_column,
            },
        }
    )
    if current_snippet != historical_snippet:
        return RegionInheritance(False, "region_source_bytes_changed", base_evidence)

    normalized_function = _normalized_function_name(current_region.function_name, current_path)
    candidates = []
    for region in historical_coverage.regions_by_path.get(historical_path, ()):
        if (
            region.start_line == historical_start
            and region.start_column == current_region.start_column
            and region.end_line == historical_end
            and region.end_column == current_region.end_column
            and _normalized_function_name(region.function_name, historical_path)
            == normalized_function
        ):
            candidates.append(region)
    base_evidence["historical_region_candidates"] = [item.as_dict() for item in candidates]
    if not candidates:
        return RegionInheritance(False, "compatible_historical_region_not_found", base_evidence)
    counts = {item.count for item in candidates}
    if len(counts) != 1:
        return RegionInheritance(False, "ambiguous_historical_region_counts", base_evidence)
    count = next(iter(counts))
    if count <= 0:
        return RegionInheritance(False, "historical_region_not_executed", base_evidence)
    base_evidence["inherited_count"] = count
    return RegionInheritance(True, "git_and_source_region_identity_match", base_evidence)


def source_region_bytes(source: str, region: CoverageRegion) -> bytes:
    """Extracts one half-open LLVM byte-column source range exactly."""

    lines = source.splitlines(keepends=True)
    if (
        region.start_line <= 0
        or region.end_line > len(lines)
        or region.end_line < region.start_line
    ):
        raise SourceRegionMappingError(
            "region_source_range_invalid", "coverage region line range exceeds Git source"
        )
    encoded = [line.encode("utf-8") for line in lines]
    start = region.start_column - 1
    end = region.end_column - 1
    if start < 0 or end < 0 or start > len(encoded[region.start_line - 1]) or end > len(
        encoded[region.end_line - 1]
    ):
        raise SourceRegionMappingError(
            "region_source_range_invalid", "coverage region column exceeds Git source"
        )
    if region.start_line == region.end_line:
        if end <= start:
            raise SourceRegionMappingError(
                "region_source_range_invalid", "coverage region has no source area"
            )
        return encoded[region.start_line - 1][start:end]
    parts = [encoded[region.start_line - 1][start:]]
    parts.extend(encoded[index] for index in range(region.start_line, region.end_line - 1))
    parts.append(encoded[region.end_line - 1][:end])
    return b"".join(parts)


def _map_equal_block(
    old_lines: Sequence[str],
    new_lines: Sequence[str],
    old_start: int,
    new_start: int,
    count: int,
    destination: Dict[int, int],
) -> None:
    """Maps one Git-declared unchanged block only when bytes are exactly equal."""

    for offset in range(count):
        old_index = old_start + offset - 1
        new_index = new_start + offset - 1
        if old_index >= len(old_lines) or new_index >= len(new_lines):
            raise SourceRegionMappingError(
                "ambiguous_line_mapping", "Git unchanged block exceeds source length"
            )
        if old_lines[old_index] != new_lines[new_index]:
            raise SourceRegionMappingError(
                "ambiguous_line_mapping", "Git unchanged block source bytes differ"
            )
        destination[new_start + offset] = old_start + offset


def _normalized_function_name(function_name: str, path: str) -> str:
    """Allows an explicit file rename to change only Clang's filename prefix."""

    basename = PurePosixPath(path).name
    prefix = basename + ":"
    if function_name.startswith(prefix):
        return function_name[len(prefix) :]
    return function_name
