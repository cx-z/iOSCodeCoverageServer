#!/usr/bin/env python3
"""Restricted cumulative source-region coverage across immutable iOS Builds."""

import hashlib
import json
import os
import re
import shutil
import tempfile
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Callable, Dict, List, Mapping, Optional, Sequence, Tuple

from ioscoverage.analysis.command_runner import EvidenceCommandRunner
from ioscoverage.analysis.mapping.coverage_export import (
    CoverageExportError,
    CoverageRegion,
    NormalizedCoverage,
    normalize_coverage_export,
    regions_for_source_line,
)
from ioscoverage.analysis.diff.git_diff_parser import (
    GitDiffParseError,
    NormalizedDiffFile,
    normalized_diff_document,
    parse_name_status_z,
    parse_unified_zero_patch,
)
from ioscoverage.integrations.git_repository import GIT_SHA_PATTERN
from ioscoverage.analysis.single_build.analyzer import AnalysisConfiguration
from ioscoverage.analysis.diff.analyzer import SingleBuildDiffAnalysisError, SingleBuildDiffAnalyzer
from ioscoverage.analysis.mapping.source_region import (
    FileLineMapping,
    SourceRegionMappingError,
    build_file_line_mapping,
    map_region_to_history,
    select_file_diff,
    source_region_bytes,
)


CYCLE_COVERAGE_SCHEMA_VERSION = 1
CYCLE_ANALYZER_VERSION = "cycle-coverage/1"
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")


class CycleCoverageAnalysisError(Exception):
    """Carries a stable cumulative-analysis error and exit category."""

    def __init__(self, code: str, message: str, exit_code: int = 3):
        """Creates one safe error without leaking tokens or device data."""

        super().__init__(message)
        self.code = code
        self.message = message
        self.exit_code = exit_code


@dataclass(frozen=True)
class BuildAnalysisInput:
    """Holds one validated Build and its immutable LLVM result."""

    build: Mapping[str, object]
    input_set_sha256: str
    export_sha256: str
    coverage: NormalizedCoverage


@dataclass(frozen=True)
class CycleCoverageResult:
    """Returns one immutable cumulative result and replay status."""

    cycle_input_set_sha256: str
    output_directory: Path
    summary: Mapping[str, object]
    idempotent_replay: bool


class CycleCoverageAnalyzer:
    """Maps historical hits to one current Build without mixing profiles."""

    def __init__(
        self,
        configuration: AnalysisConfiguration,
        command_runner_factory: Callable[[Path, Path], object] = EvidenceCommandRunner,
    ):
        """Uses fixed installation paths and an injectable no-shell runner."""

        self.configuration = configuration
        self.command_runner_factory = command_runner_factory
        self.analysis_root = configuration.analysis_root.parent / "cycle-coverage"
        self.input_validator = SingleBuildDiffAnalyzer(configuration)

    def analyze(
        self,
        cycle_id: str,
        current_build_id: str,
        build_inputs: Mapping[str, str],
    ) -> CycleCoverageResult:
        """Validates, maps, accumulates, and atomically publishes one Cycle snapshot."""

        started_at = self._utc_now()
        cycle_id = self._normalize_uuid(cycle_id, "cycle_id")
        current_build_id = self._normalize_uuid(current_build_id, "current_build_id")
        normalized_inputs = self._normalize_build_inputs(build_inputs)
        if current_build_id not in normalized_inputs:
            raise CycleCoverageAnalysisError(
                "current_build_input_required", "build inputs must include current_build_id"
            )
        staging_dir = self._make_staging_directory(current_build_id)
        runner = self.command_runner_factory(staging_dir, self.configuration.developer_dir)
        cycle_input_sha: Optional[str] = None
        try:
            inputs = self._load_build_inputs(cycle_id, normalized_inputs)
            current = next(item for item in inputs if item.build["id"] == current_build_id)
            projects = {str(item.build["project"]) for item in inputs}
            if len(projects) != 1:
                raise CycleCoverageAnalysisError("build_cycle_mismatch", "Build projects are inconsistent")
            repository_path = self.configuration.project_repository_path(current.build["project"])
            self._validate_build_order(inputs, current)
            self._validate_git_builds(runner, inputs, repository_path)

            current_diff, current_diff_sha = self._git_diff(
                runner,
                "current",
                str(current.build["merge_base_sha"]),
                str(current.build["git_sha"]),
                repository_path,
            )
            historical_inputs = [item for item in inputs if item.build["id"] != current_build_id]
            history_diffs: Dict[str, Tuple[List[NormalizedDiffFile], str]] = {}
            for index, item in enumerate(historical_inputs, start=1):
                history_diffs[str(item.build["id"])] = self._git_diff(
                    runner,
                    "history-{:02d}".format(index),
                    str(item.build["git_sha"]),
                    str(current.build["git_sha"]),
                    repository_path,
                )

            lines, file_summaries, region_mappings = self._accumulate(
                runner, current, historical_inputs, current_diff, history_diffs, repository_path
            )
            mapping_evidence = {
                "schema_version": CYCLE_COVERAGE_SCHEMA_VERSION,
                "current_denominator": {
                    "merge_base_sha": current.build["merge_base_sha"],
                    "git_sha": current.build["git_sha"],
                    "normalized_diff_sha256": current_diff_sha,
                },
                "historical_diffs": [
                    {
                        "build_id": item.build["id"],
                        "git_sha": item.build["git_sha"],
                        "current_git_sha": current.build["git_sha"],
                        "normalized_diff_sha256": history_diffs[str(item.build["id"])][1],
                    }
                    for item in historical_inputs
                ],
                "region_mappings": region_mappings,
            }
            mapping_sha = self._json_sha256(mapping_evidence)
            identity = {
                "schema_version": CYCLE_COVERAGE_SCHEMA_VERSION,
                "analyzer_version": CYCLE_ANALYZER_VERSION,
                "cycle_id": cycle_id,
                "current_build": self._build_identity(current),
                "build_inputs": [self._build_identity(item) for item in inputs],
                "current_denominator_diff_sha256": current_diff_sha,
                "mapping_evidence_sha256": mapping_sha,
            }
            cycle_input_sha = self._json_sha256(identity)
            manifest = {**identity, "cycle_input_set_sha256": cycle_input_sha}
            final_dir = self._final_directory(cycle_id, current_build_id, cycle_input_sha)
            if final_dir.exists():
                summary = self._validate_existing_result(final_dir, manifest, cycle_input_sha)
                shutil.rmtree(str(staging_dir), ignore_errors=True)
                return CycleCoverageResult(cycle_input_sha, final_dir, summary, True)

            completed_at = self._utc_now()
            summary = self._build_summary(
                cycle_id,
                current,
                inputs,
                cycle_input_sha,
                current_diff_sha,
                mapping_sha,
                lines,
                file_summaries,
                started_at,
                completed_at,
            )
            self._write_json(staging_dir / "manifest.json", manifest)
            self._write_json(
                staging_dir / "cumulative-line-coverage.json",
                {
                    "schema_version": CYCLE_COVERAGE_SCHEMA_VERSION,
                    "cycle_input_set_sha256": cycle_input_sha,
                    "lines": lines,
                },
            )
            self._write_json(staging_dir / "mapping-evidence.json", mapping_evidence)
            self._write_json(staging_dir / "summary.json", summary)
            self._write_json(staging_dir / "commands.json", {"commands": runner.records})
            completion = {
                "schema_version": CYCLE_COVERAGE_SCHEMA_VERSION,
                "status": "complete",
                "cycle_input_set_sha256": cycle_input_sha,
                "completed_at": completed_at,
                "outputs": self._directory_file_identity(staging_dir),
            }
            self._write_json(staging_dir / "complete.json", completion)
            self._fsync_directory(staging_dir)
            final_dir.parent.mkdir(parents=True, exist_ok=True)
            try:
                os.rename(str(staging_dir), str(final_dir))
            except OSError:
                if not final_dir.exists():
                    raise
                existing = self._validate_existing_result(final_dir, manifest, cycle_input_sha)
                shutil.rmtree(str(staging_dir), ignore_errors=True)
                return CycleCoverageResult(cycle_input_sha, final_dir, existing, True)
            self._fsync_directory(final_dir.parent)
            return CycleCoverageResult(cycle_input_sha, final_dir, summary, False)
        except CycleCoverageAnalysisError as error:
            self._publish_failure(
                staging_dir, cycle_id, current_build_id, cycle_input_sha, runner, error
            )
            raise
        except (
            SingleBuildDiffAnalysisError,
            CoverageExportError,
            GitDiffParseError,
            SourceRegionMappingError,
        ) as error:
            wrapped = CycleCoverageAnalysisError(error.code, error.message)
            self._publish_failure(
                staging_dir, cycle_id, current_build_id, cycle_input_sha, runner, wrapped
            )
            raise wrapped from error
        except Exception as error:
            wrapped = CycleCoverageAnalysisError(
                "cycle_analysis_internal_error", "Cycle coverage analysis failed unexpectedly", 5
            )
            self._publish_failure(
                staging_dir, cycle_id, current_build_id, cycle_input_sha, runner, wrapped
            )
            raise wrapped from error

    def _load_build_inputs(
        self, cycle_id: str, build_inputs: Mapping[str, str]
    ) -> List[BuildAnalysisInput]:
        """Validates every Build and prior single-Build LLVM result without writes."""

        result: List[BuildAnalysisInput] = []
        for build_id, input_sha in build_inputs.items():
            build, _ = self.input_validator.load_and_validate_build(cycle_id, build_id)
            single_dir = self.configuration.analysis_root / build_id / input_sha
            _, _, export_path, export_sha = self.input_validator.validate_single_build_result(
                single_dir, build, input_sha
            )
            try:
                payload = json.loads(export_path.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
                raise CycleCoverageAnalysisError(
                    "coverage_export_invalid", "llvm-cov export is missing or invalid"
                ) from error
            repository_path = self.configuration.project_repository_path(build["project"])
            coverage = normalize_coverage_export(payload, repository_path)
            result.append(BuildAnalysisInput(build, input_sha, export_sha, coverage))
        result.sort(key=lambda item: (self._build_time(item), str(item.build["id"])))
        return result

    def _validate_build_order(
        self, inputs: Sequence[BuildAnalysisInput], current: BuildAnalysisInput
    ) -> None:
        """Rejects selected Builds that were registered after the current Build."""

        current_time = self._build_time(current)
        for item in inputs:
            if self._build_time(item) > current_time:
                raise CycleCoverageAnalysisError(
                    "future_build_input", "build input was created after current_build_id"
                )

    def _validate_git_builds(
        self,
        runner: object,
        inputs: Sequence[BuildAnalysisInput],
        repository_path: Path,
    ) -> None:
        """Records the fixed repository root and each immutable Build snapshot."""

        repository_path = repository_path.resolve()
        repository = str(repository_path)
        if not repository_path.is_dir():
            raise CycleCoverageAnalysisError(
                "git_repository_invalid", "configured project repository is unavailable"
            )
        top_level = self._run_git(
            runner,
            "git-repository-root",
            ["git", "-C", repository, "rev-parse", "--show-toplevel"],
        ).stdout.strip()
        if not top_level or Path(top_level).resolve() != repository_path:
            raise CycleCoverageAnalysisError(
                "git_repository_invalid", "configured project repository is not the Git worktree root"
            )
        for build_index, item in enumerate(inputs, start=1):
            build = item.build
            for sha_index, field in enumerate(
                ("git_sha", "target_sha_at_build", "merge_base_sha"), start=1
            ):
                sha = str(build[field])
                if not GIT_SHA_PATTERN.fullmatch(sha):
                    raise CycleCoverageAnalysisError("invalid_git_sha", field + " is not a full SHA")
                self._run_git(
                    runner,
                    "git-build-{:02d}-cat-{:02d}".format(build_index, sha_index),
                    ["git", "-C", repository, "cat-file", "-e", sha + "^{commit}"],
                )
            actual = self._run_git(
                runner,
                "git-build-{:02d}-merge-base".format(build_index),
                [
                    "git",
                    "-C",
                    repository,
                    "merge-base",
                    str(build["target_sha_at_build"]),
                    str(build["git_sha"]),
                ],
            ).stdout.strip().lower()
            if actual != build["merge_base_sha"]:
                raise CycleCoverageAnalysisError(
                    "merge_base_mismatch", "Build merge_base_sha no longer validates"
                )

    def _git_diff(
        self, runner: object, label_prefix: str, old_sha: str, new_sha: str, repository_path: Path
    ) -> Tuple[List[NormalizedDiffFile], str]:
        """Generates one rename-aware fixed-object diff and semantic identity."""

        repository = str(repository_path.resolve())
        status = self._run_git(
            runner,
            "git-" + label_prefix + "-status",
            [
                "git",
                "-c",
                "core.quotePath=false",
                "-C",
                repository,
                "diff",
                "--name-status",
                "-z",
                "--find-renames",
                "--no-ext-diff",
                "--no-textconv",
                old_sha,
                new_sha,
                "--",
            ],
        )
        statuses = parse_name_status_z(status.stdout)
        patch_name = "git-" + label_prefix + "-diff.patch"
        self._run_git(
            runner,
            "git-" + label_prefix + "-diff",
            [
                "git",
                "-c",
                "core.quotePath=false",
                "-C",
                repository,
                "diff",
                "--unified=0",
                "--no-color",
                "--no-ext-diff",
                "--no-textconv",
                "--find-renames",
                old_sha,
                new_sha,
                "--",
            ],
            stdout_filename=patch_name,
        )
        try:
            patch = (runner.evidence_dir / patch_name).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as error:
            raise CycleCoverageAnalysisError("git_diff_invalid", "Git patch is unreadable") from error
        files = parse_unified_zero_patch(patch, statuses)
        return files, self._json_sha256(normalized_diff_document(files))

    def _accumulate(
        self,
        runner: object,
        current: BuildAnalysisInput,
        histories: Sequence[BuildAnalysisInput],
        current_diff: Sequence[NormalizedDiffFile],
        history_diffs: Mapping[str, Tuple[List[NormalizedDiffFile], str]],
        repository_path: Path,
    ) -> Tuple[List[Dict[str, object]], List[Dict[str, object]], List[Dict[str, object]]]:
        """Classifies current changed lines and resolves every zero region conservatively."""

        source_cache: Dict[Tuple[str, str], str] = {}
        file_mapping_cache: Dict[Tuple[str, str], FileLineMapping] = {}
        region_cache: Dict[Tuple[str, CoverageRegion], Dict[str, object]] = {}
        region_mappings: List[Dict[str, object]] = []
        lines: List[Dict[str, object]] = []
        file_summaries: List[Dict[str, object]] = []
        source_counter = 0

        def load_source(sha: str, path: str) -> str:
            """Loads each Git object source once and records it as command evidence."""

            nonlocal source_counter
            key = (sha, path)
            if key not in source_cache:
                source_counter += 1
                result = self._run_git(
                    runner,
                    "git-source-{:04d}".format(source_counter),
                    [
                        "git",
                        "-C",
                        str(repository_path.resolve()),
                        "show",
                        sha + ":" + path,
                    ],
                )
                source_cache[key] = result.stdout
            return source_cache[key]

        current_sha = str(current.build["git_sha"])
        for diff_file in current_diff:
            path = diff_file.new_path or diff_file.old_path
            if diff_file.new_path is None or diff_file.binary or not diff_file.changed_new_lines:
                file_summaries.append(
                    self._file_summary(path, diff_file, [])
                )
                continue
            current_path = diff_file.new_path
            current_source = load_source(current_sha, current_path)
            source_lines = current_source.splitlines()
            file_lines: List[Dict[str, object]] = []
            for line_number in diff_file.changed_new_lines:
                if current_path in current.coverage.conflicted_paths:
                    state = "unknown"
                    reason = current.coverage.conflicted_paths[current_path]
                    related: Sequence[CoverageRegion] = ()
                    resolutions: List[Dict[str, object]] = []
                else:
                    current_regions = current.coverage.regions_by_path.get(current_path)
                    if current_regions is None:
                        related = ()
                        resolutions = []
                        if line_number <= 0 or line_number > len(source_lines):
                            state, reason = "unknown", "git_changed_line_out_of_range"
                        else:
                            state, reason = "not_measurable", "no_compiler_coverage_mapping_for_path"
                    else:
                        related, range_error = regions_for_source_line(
                            current_regions, line_number, source_lines
                        )
                        if range_error:
                            state, reason, resolutions = "unknown", range_error, []
                        elif not related:
                            state, reason, resolutions = (
                                "not_measurable",
                                "no_measurable_non_gap_region",
                                [],
                            )
                        else:
                            resolutions = []
                            for region in related:
                                cache_key = (current_path, region)
                                if cache_key not in region_cache:
                                    resolution = self._resolve_region(
                                        region,
                                        current_path,
                                        current_source,
                                        current,
                                        histories,
                                        history_diffs,
                                        load_source,
                                        file_mapping_cache,
                                    )
                                    region_cache[cache_key] = resolution
                                    region_mappings.append(resolution)
                                resolutions.append(region_cache[cache_key])
                            if all(bool(item["covered"]) for item in resolutions):
                                state, reason = "covered", "all_current_regions_have_hit_evidence"
                            else:
                                state, reason = "uncovered", "at_least_one_current_region_has_no_hit_evidence"
                source_text = source_lines[line_number - 1] if 0 < line_number <= len(source_lines) else ""
                record = {
                    "repo_relative_path": current_path,
                    "git_status": diff_file.status,
                    "old_path": diff_file.old_path,
                    "new_path": current_path,
                    "new_line_number": line_number,
                    "source_text": source_text,
                    "source_text_sha256": hashlib.sha256(source_text.encode("utf-8")).hexdigest(),
                    "state": state,
                    "reason": reason,
                    "regions": resolutions,
                }
                lines.append(record)
                file_lines.append(record)
            file_summaries.append(self._file_summary(path, diff_file, file_lines))
        lines.sort(key=lambda item: (str(item["repo_relative_path"]), int(item["new_line_number"])))
        region_mappings.sort(key=self._region_mapping_sort_key)
        return lines, file_summaries, region_mappings

    def _resolve_region(
        self,
        region: CoverageRegion,
        current_path: str,
        current_source: str,
        current: BuildAnalysisInput,
        histories: Sequence[BuildAnalysisInput],
        history_diffs: Mapping[str, Tuple[List[NormalizedDiffFile], str]],
        source_loader: Callable[[str, str], str],
        file_mapping_cache: Dict[Tuple[str, str], FileLineMapping],
    ) -> Dict[str, object]:
        """Uses the current hit first, then the newest compatible historical hit."""

        current_snippet_sha = hashlib.sha256(source_region_bytes(current_source, region)).hexdigest()
        base = {
            "repo_relative_path": current_path,
            "current_region": region.as_dict(),
            "current_source_range_sha256": current_snippet_sha,
            "covered": region.count > 0,
            "source": {
                "kind": "current_build",
                "build_id": current.build["id"],
                "input_set_sha256": current.input_set_sha256,
                "git_sha": current.build["git_sha"],
            }
            if region.count > 0
            else None,
            "inheritance_attempts": [],
        }
        if region.count > 0:
            base["reason"] = "current_build_region_count_positive"
            return base
        for historical in reversed(histories):
            build_id = str(historical.build["id"])
            diff_files = history_diffs[build_id][0]
            diff_file = select_file_diff(current_path, diff_files)
            historical_path = diff_file.old_path if diff_file is not None else current_path
            historical_source: Optional[str] = None
            if diff_file is None or diff_file.status != "A":
                if historical_path is not None:
                    historical_source = source_loader(str(historical.build["git_sha"]), historical_path)
            mapping_key = (build_id, current_path)
            if mapping_key not in file_mapping_cache:
                file_mapping_cache[mapping_key] = build_file_line_mapping(
                    current_path, historical_source, current_source, diff_file
                )
            mapping = file_mapping_cache[mapping_key]
            if historical_source is None:
                attempt = {
                    "inherited": False,
                    "reason": mapping.reason,
                    "evidence": {
                        "current_path": current_path,
                        "historical_path": mapping.historical_path,
                        "git_status": mapping.git_status,
                    },
                }
            else:
                inheritance = map_region_to_history(
                    current_path,
                    region,
                    current_source,
                    historical_source,
                    mapping,
                    historical.coverage,
                )
                attempt = {
                    "inherited": inheritance.inherited,
                    "reason": inheritance.reason,
                    "evidence": inheritance.evidence,
                }
            attempt.update(
                {
                    "build_id": build_id,
                    "input_set_sha256": historical.input_set_sha256,
                    "git_sha": historical.build["git_sha"],
                }
            )
            base["inheritance_attempts"].append(attempt)
            if attempt["inherited"]:
                base["covered"] = True
                base["reason"] = "inherited_compatible_historical_hit"
                base["source"] = {
                    "kind": "inherited_build",
                    "build_id": build_id,
                    "input_set_sha256": historical.input_set_sha256,
                    "git_sha": historical.build["git_sha"],
                    "historical_path": attempt["evidence"].get("historical_path"),
                    "historical_region_range": attempt["evidence"].get(
                        "historical_region_range"
                    ),
                    "current_region_range": region.as_dict(),
                    "source_range_sha256": attempt["evidence"].get(
                        "current_source_range_sha256"
                    ),
                    "mapping_reason": attempt["reason"],
                }
                return base
        base["reason"] = "current_zero_and_no_compatible_historical_hit"
        return base

    def _file_summary(
        self,
        path: Optional[str],
        diff_file: NormalizedDiffFile,
        lines: Sequence[Mapping[str, object]],
    ) -> Dict[str, object]:
        """Builds one deterministic current-file state summary."""

        states = ("covered", "uncovered", "not_measurable", "unknown")
        counts = {state: sum(1 for line in lines if line["state"] == state) for state in states}
        return {
            "repo_relative_path": path,
            "git_status": diff_file.status,
            "binary": diff_file.binary,
            "changed_new_side": len(lines),
            "measurable": counts["covered"] + counts["uncovered"],
            **counts,
            "deleted_excluded": diff_file.deleted_excluded,
        }

    def _build_summary(
        self,
        cycle_id: str,
        current: BuildAnalysisInput,
        inputs: Sequence[BuildAnalysisInput],
        cycle_input_sha: str,
        current_diff_sha: str,
        mapping_sha: str,
        lines: Sequence[Mapping[str, object]],
        files: Sequence[Mapping[str, object]],
        started_at: str,
        completed_at: str,
    ) -> Dict[str, object]:
        """Computes cumulative current-line totals with a transparent denominator."""

        states = ("covered", "uncovered", "not_measurable", "unknown")
        counts = {state: sum(1 for line in lines if line["state"] == state) for state in states}
        measurable = counts["covered"] + counts["uncovered"]
        percent = counts["covered"] / measurable * 100.0 if measurable else None
        inherited_lines = sum(
            1
            for line in lines
            if line["state"] == "covered"
            and any(
                region.get("source", {}).get("kind") == "inherited_build"
                for region in line.get("regions", [])
                if isinstance(region.get("source"), dict)
            )
        )
        return {
            "schema_version": CYCLE_COVERAGE_SCHEMA_VERSION,
            "status": "complete",
            "cycle_id": cycle_id,
            "current_build_id": current.build["id"],
            "current_git_sha": current.build["git_sha"],
            "target_sha_at_build": current.build["target_sha_at_build"],
            "merge_base_sha": current.build["merge_base_sha"],
            "cycle_input_set_sha256": cycle_input_sha,
            "current_denominator_diff_sha256": current_diff_sha,
            "mapping_evidence_sha256": mapping_sha,
            "build_ids": [item.build["id"] for item in inputs],
            "build_count": len(inputs),
            "files": {
                "changed": len(files),
                "with_measurable_changes": sum(1 for item in files if item["measurable"] > 0),
                "only_not_measurable": sum(
                    1
                    for item in files
                    if item["changed_new_side"] > 0
                    and item["not_measurable"] == item["changed_new_side"]
                ),
                "with_unknown": sum(1 for item in files if item["unknown"] > 0),
            },
            "lines": {
                "changed_new_side": len(lines),
                "measurable": measurable,
                **counts,
                "deleted_excluded": sum(int(item["deleted_excluded"]) for item in files),
                "covered_with_inherited_evidence": inherited_lines,
            },
            "changed_line_coverage_percent": percent,
            "per_file": list(files),
            "started_at": started_at,
            "completed_at": completed_at,
            "warnings": [],
            "granularity": (
                "Current Build Git new-side lines and compiler regions with boolean hits; historical "
                "hits require exact Git/source-region identity and never represent QA execution counts."
            ),
        }

    def _build_identity(self, item: BuildAnalysisInput) -> Dict[str, object]:
        """Returns the stable Build/input/export identity used by cycle hashing."""

        return {
            "build_id": item.build["id"],
            "created_at": item.build["created_at"],
            "git_sha": item.build["git_sha"],
            "target_sha_at_build": item.build["target_sha_at_build"],
            "merge_base_sha": item.build["merge_base_sha"],
            "coverage_binary": item.build["coverage_binary"],
            "manifest_sha256": item.build["manifest_sha256"],
            "input_set_sha256": item.input_set_sha256,
            "llvm_cov_export_sha256": item.export_sha256,
        }

    def _build_time(self, item: BuildAnalysisInput) -> datetime:
        """Parses one persisted timezone-aware Build creation timestamp."""

        value = item.build.get("created_at")
        if not isinstance(value, str):
            raise CycleCoverageAnalysisError("build_created_at_invalid", "Build created_at is missing")
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as error:
            raise CycleCoverageAnalysisError(
                "build_created_at_invalid", "Build created_at is invalid"
            ) from error
        if parsed.tzinfo is None:
            raise CycleCoverageAnalysisError(
                "build_created_at_invalid", "Build created_at must include a timezone"
            )
        return parsed

    def _normalize_build_inputs(self, build_inputs: Mapping[str, str]) -> Dict[str, str]:
        """Normalizes every Build UUID and input SHA without allowing duplicates."""

        result: Dict[str, str] = {}
        for raw_build_id, raw_input_sha in build_inputs.items():
            build_id = self._normalize_uuid(raw_build_id, "build_input build_id")
            input_sha = self._normalize_sha256(raw_input_sha, "build_input input_set_sha256")
            if build_id in result:
                raise CycleCoverageAnalysisError("duplicate_build_input", "Build input is duplicated")
            result[build_id] = input_sha
        if not result:
            raise CycleCoverageAnalysisError("build_input_required", "at least one build input is required")
        return result

    def _run_git(
        self,
        runner: object,
        label: str,
        arguments: Sequence[str],
        stdout_filename: Optional[str] = None,
    ) -> object:
        """Runs one fixed Git command and rejects nonzero exit or any stderr."""

        result = runner.run(label, arguments, stdout_filename=stdout_filename)
        if result.returncode != 0:
            raise CycleCoverageAnalysisError(
                "git_command_failed", label + " exited with code " + str(result.returncode), 4
            )
        if result.stderr:
            raise CycleCoverageAnalysisError("git_stderr_not_empty", label + " emitted stderr", 4)
        return result

    def _validate_existing_result(
        self, directory: Path, manifest: Mapping[str, object], cycle_input_sha: str
    ) -> Mapping[str, object]:
        """Reuses only an exact completion-protected cumulative result."""

        stored_manifest = self._load_json(directory / "manifest.json", "existing_cycle_result_invalid")
        summary = self._load_json(directory / "summary.json", "existing_cycle_result_invalid")
        completion = self._load_json(directory / "complete.json", "existing_cycle_result_invalid")
        if stored_manifest != manifest:
            raise CycleCoverageAnalysisError(
                "existing_cycle_result_conflict", "existing Cycle result has different inputs", 5
            )
        if (
            completion.get("status") != "complete"
            or completion.get("cycle_input_set_sha256") != cycle_input_sha
            or summary.get("status") != "complete"
            or summary.get("cycle_input_set_sha256") != cycle_input_sha
        ):
            raise CycleCoverageAnalysisError(
                "existing_cycle_result_invalid", "existing Cycle completion is invalid", 5
            )
        outputs = completion.get("outputs")
        if not isinstance(outputs, dict):
            raise CycleCoverageAnalysisError(
                "existing_cycle_result_invalid", "existing Cycle output identity is missing", 5
            )
        actual = {
            path.relative_to(directory).as_posix()
            for path in directory.rglob("*")
            if path.is_file() and path.name != "complete.json"
        }
        if actual != set(outputs):
            raise CycleCoverageAnalysisError(
                "existing_cycle_result_invalid", "existing Cycle output set has changed", 5
            )
        for relative, descriptor in outputs.items():
            safe = self._safe_relative_name(relative)
            if not isinstance(descriptor, dict):
                raise CycleCoverageAnalysisError(
                    "existing_cycle_result_invalid", "existing Cycle output identity is invalid", 5
                )
            size, sha = self._file_identity(directory / safe, "Cycle analysis output")
            if size != descriptor.get("size") or sha != descriptor.get("sha256"):
                raise CycleCoverageAnalysisError(
                    "existing_cycle_result_invalid", "existing Cycle output bytes changed", 5
                )
        return summary

    def _publish_failure(
        self,
        staging_dir: Path,
        cycle_id: str,
        current_build_id: str,
        cycle_input_sha: Optional[str],
        runner: object,
        error: CycleCoverageAnalysisError,
    ) -> None:
        """Preserves failures outside the successful identity namespace."""

        if not staging_dir.exists():
            return
        try:
            if not (staging_dir / "commands.json").exists():
                self._write_json(staging_dir / "commands.json", {"commands": runner.records})
            self._write_json(
                staging_dir / "failure.json",
                {
                    "schema_version": CYCLE_COVERAGE_SCHEMA_VERSION,
                    "status": "failed",
                    "cycle_id": cycle_id,
                    "current_build_id": current_build_id,
                    "cycle_input_set_sha256": cycle_input_sha,
                    "error": error.code,
                    "message": error.message,
                    "failed_at": self._utc_now(),
                },
            )
            self._fsync_directory(staging_dir)
            parent = self.analysis_root / ".failed" / cycle_id / current_build_id
            parent.mkdir(parents=True, exist_ok=True)
            destination = parent / (self._utc_compact() + "-" + uuid.uuid4().hex)
            os.rename(str(staging_dir), str(destination))
            self._fsync_directory(parent)
        except Exception:
            shutil.rmtree(str(staging_dir), ignore_errors=True)

    def _final_directory(self, cycle_id: str, current_build_id: str, identity: str) -> Path:
        """Returns one immutable cumulative success directory."""

        return self.analysis_root / cycle_id / current_build_id / identity

    def _make_staging_directory(self, current_build_id: str) -> Path:
        """Creates one unpublished same-filesystem staging directory."""

        root = self.analysis_root / ".staging"
        root.mkdir(parents=True, exist_ok=True)
        return Path(tempfile.mkdtemp(prefix="." + current_build_id + "-", dir=str(root)))

    def _directory_file_identity(self, directory: Path) -> Dict[str, Dict[str, object]]:
        """Hashes all staged evidence before complete.json is written."""

        result = {}
        for path in sorted(item for item in directory.rglob("*") if item.is_file()):
            size, sha = self._file_identity(path, "Cycle analysis output")
            result[path.relative_to(directory).as_posix()] = {"size": size, "sha256": sha}
        return result

    def _file_identity(self, path: Path, label: str) -> Tuple[int, str]:
        """Hashes one required regular non-symlink file."""

        if not path.is_file() or path.is_symlink():
            raise CycleCoverageAnalysisError("stored_file_missing", label + " is missing")
        digest = hashlib.sha256()
        size = 0
        try:
            with path.open("rb") as source:
                while True:
                    chunk = source.read(1024 * 1024)
                    if not chunk:
                        break
                    size += len(chunk)
                    digest.update(chunk)
        except OSError as error:
            raise CycleCoverageAnalysisError("stored_file_unreadable", label + " cannot be read") from error
        return size, digest.hexdigest()

    def _load_json(self, path: Path, code: str) -> Dict[str, object]:
        """Loads one required JSON object."""

        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
            raise CycleCoverageAnalysisError(code, path.name + " is missing or invalid") from error
        if not isinstance(payload, dict):
            raise CycleCoverageAnalysisError(code, path.name + " must contain an object")
        return payload

    def _safe_relative_name(self, value: object) -> str:
        """Rejects completion paths capable of escaping their result directory."""

        if not isinstance(value, str) or not value:
            raise CycleCoverageAnalysisError("result_output_path_invalid", "output path is invalid", 5)
        path = PurePosixPath(value)
        if path.is_absolute() or path.as_posix() != value or ".." in path.parts:
            raise CycleCoverageAnalysisError("result_output_path_invalid", "output path is unsafe", 5)
        return value

    def _write_json(self, path: Path, payload: object) -> None:
        """Writes deterministic JSON and fsyncs it before publication."""

        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2).encode(
            "utf-8"
        ) + b"\n"
        with path.open("xb") as output:
            output.write(encoded)
            output.flush()
            os.fsync(output.fileno())

    def _fsync_directory(self, directory: Path) -> None:
        """Flushes one directory entry."""

        descriptor = os.open(str(directory), os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def _normalize_uuid(self, value: str, field: str) -> str:
        """Normalizes a UUID before filesystem use."""

        try:
            return str(uuid.UUID(value))
        except (AttributeError, ValueError) as error:
            raise CycleCoverageAnalysisError("invalid_uuid", field + " must be a UUID") from error

    def _normalize_sha256(self, value: str, field: str) -> str:
        """Accepts one lowercase full SHA-256."""

        if not isinstance(value, str) or not SHA256_PATTERN.fullmatch(value):
            raise CycleCoverageAnalysisError("invalid_sha256", field + " must be lowercase SHA-256")
        return value

    def _json_sha256(self, payload: object) -> str:
        """Hashes canonical semantic JSON."""

        return hashlib.sha256(
            json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
                "utf-8"
            )
        ).hexdigest()

    def _region_mapping_sort_key(self, item: Mapping[str, object]) -> Tuple[object, ...]:
        """Sorts current region decisions independently of traversal order."""

        region = item["current_region"]
        return (
            item["repo_relative_path"],
            region["start_line"],
            region["start_column"],
            region["end_line"],
            region["end_column"],
            region["function_name"],
            region["count"],
        )

    def _utc_now(self) -> str:
        """Returns one timezone-aware UTC timestamp."""

        return datetime.now(timezone.utc).isoformat()

    def _utc_compact(self) -> str:
        """Returns one filesystem-safe UTC timestamp."""

        return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
