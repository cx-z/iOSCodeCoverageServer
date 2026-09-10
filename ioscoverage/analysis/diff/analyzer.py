#!/usr/bin/env python3
"""Audited changed-line coverage analysis for one immutable iOS Build result."""

import hashlib
import json
import os
import re
import shutil
import sqlite3
import tempfile
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Callable, Dict, List, Mapping, Optional, Sequence, Tuple
from urllib.parse import quote

from ioscoverage.analysis.command_runner import EvidenceCommandRunner
from ioscoverage.analysis.mapping.coverage_export import (
    CoverageExportError,
    NormalizedCoverage,
    normalize_coverage_export,
    regions_for_source_line,
)
from ioscoverage.storage.coverage_store import ARTIFACT_STORAGE_NAMES
from ioscoverage.analysis.diff.git_diff_parser import (
    GitDiffParseError,
    NormalizedDiffFile,
    normalized_diff_document,
    parse_name_status_z,
    parse_unified_zero_patch,
)
from ioscoverage.integrations.git_repository import GIT_SHA_PATTERN
from ioscoverage.analysis.single_build.analyzer import AnalysisConfiguration
from ioscoverage.storage.retained_evidence import RetainedBuildEvidenceStore


DIFF_ANALYSIS_SCHEMA_VERSION = 1
DIFF_ANALYZER_VERSION = "single-build-diff/2"
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
SUPPORTED_COVERAGE_BINARIES = ("main_executable", "debug_dylib")


class SingleBuildDiffAnalysisError(Exception):
    """Carries a stable CLI error and process exit category."""

    def __init__(self, code: str, message: str, exit_code: int = 3):
        """Creates one safe failure without leaking credentials or arbitrary paths."""

        super().__init__(message)
        self.code = code
        self.message = message
        self.exit_code = exit_code


@dataclass(frozen=True)
class DiffAnalysisResult:
    """Returns one immutable changed-line result directory."""

    diff_set_sha256: str
    output_directory: Path
    summary: Mapping[str, object]
    idempotent_replay: bool


class SingleBuildDiffAnalyzer:
    """Intersects one verified llvm-cov export with one fixed Git snapshot diff."""

    def __init__(
        self,
        configuration: AnalysisConfiguration,
        command_runner_factory: Callable[[Path, Path], object] = EvidenceCommandRunner,
        retained_evidence_store: Optional[RetainedBuildEvidenceStore] = None,
    ):
        """Retains only installation-owned paths and an injectable no-shell runner."""

        self.configuration = configuration
        self.command_runner_factory = command_runner_factory
        self.diff_analysis_root = configuration.analysis_root.parent / "single-build-diff"
        self.retained_evidence_store = retained_evidence_store or RetainedBuildEvidenceStore(
            configuration.analysis_root.parent
        )

    def analyze(
        self, cycle_id: str, build_id: str, input_set_sha256: str
    ) -> DiffAnalysisResult:
        """Validates, computes, and atomically publishes one fixed changed-line report."""

        started_at = self._utc_now()
        cycle_id = self._normalize_uuid(cycle_id, "cycle_id")
        build_id = self._normalize_uuid(build_id, "build_id")
        input_set_sha256 = self._normalize_sha256(input_set_sha256, "input_set_sha256")
        staging_dir = self._make_staging_directory(build_id)
        runner = self.command_runner_factory(staging_dir, self.configuration.developer_dir)
        diff_set_sha256: Optional[str] = None
        try:
            build, _ = self.load_and_validate_build(cycle_id, build_id)
            repository_path = self.configuration.project_repository_path(build["project"])
            single_dir = self._single_build_directory(build_id, input_set_sha256)
            _, single_summary, export_path, export_sha256 = (
                self.validate_single_build_result(
                    single_dir, build, input_set_sha256
                )
            )
            export_payload = self._load_json(export_path, "coverage_export_invalid")
            try:
                normalized_coverage = normalize_coverage_export(
                    export_payload, repository_path
                )
            except CoverageExportError as error:
                raise SingleBuildDiffAnalysisError(error.code, error.message) from error

            self._validate_git_snapshot(runner, build, repository_path)
            normalized_files = self._generate_git_diff(runner, build, repository_path)
            normalized_diff = normalized_diff_document(normalized_files)
            normalized_diff_sha256 = self._json_sha256(normalized_diff)
            identity = {
                "schema_version": DIFF_ANALYSIS_SCHEMA_VERSION,
                "cycle_id": cycle_id,
                "build_id": build_id,
                "manifest_sha256": build["manifest_sha256"],
                "input_set_sha256": input_set_sha256,
                "llvm_cov_export_sha256": export_sha256,
                "merge_base_sha": build["merge_base_sha"],
                "git_sha": build["git_sha"],
                "normalized_git_diff_sha256": normalized_diff_sha256,
                "diff_analyzer_version": DIFF_ANALYZER_VERSION,
            }
            diff_set_sha256 = self._json_sha256(identity)
            inputs = {
                **identity,
                "diff_set_sha256": diff_set_sha256,
                "target_sha_at_build": build["target_sha_at_build"],
                "coverage_binary": build["coverage_binary"],
                "single_build_result": {
                    "schema_version": single_summary["schema_version"],
                    "completion_status": "complete",
                    "run_ids": single_summary["run_ids"],
                },
            }
            final_dir = self._final_directory(build_id, input_set_sha256, diff_set_sha256)
            if final_dir.exists():
                summary = self._validate_existing_result(final_dir, inputs, diff_set_sha256)
                shutil.rmtree(str(staging_dir), ignore_errors=True)
                return DiffAnalysisResult(diff_set_sha256, final_dir, summary, True)

            changed_lines, git_files = self._classify_changed_lines(
                runner, normalized_files, normalized_coverage, str(build["git_sha"]), repository_path
            )
            completed_at = self._utc_now()
            summary = self._build_summary(
                build,
                input_set_sha256,
                diff_set_sha256,
                export_sha256,
                normalized_diff_sha256,
                normalized_files,
                changed_lines,
                git_files,
                started_at,
                completed_at,
                normalized_coverage,
            )
            self._write_json(staging_dir / "inputs.json", inputs)
            self._write_json(staging_dir / "git-files.json", {"files": git_files})
            self._write_json(staging_dir / "normalized-diff.json", normalized_diff)
            self._write_json(
                staging_dir / "normalized-coverage.json", normalized_coverage.document()
            )
            self._write_json(
                staging_dir / "changed-line-coverage.json",
                {
                    "schema_version": DIFF_ANALYSIS_SCHEMA_VERSION,
                    "diff_set_sha256": diff_set_sha256,
                    "lines": changed_lines,
                },
            )
            self._write_json(staging_dir / "summary.json", summary)
            self._write_json(staging_dir / "commands.json", {"commands": runner.records})
            output_identity = self._directory_file_identity(staging_dir)
            completion = {
                "schema_version": DIFF_ANALYSIS_SCHEMA_VERSION,
                "status": "complete",
                "diff_set_sha256": diff_set_sha256,
                "completed_at": completed_at,
                "outputs": output_identity,
            }
            self._write_json(staging_dir / "completion.json", completion)
            self._fsync_directory(staging_dir)
            final_dir.parent.mkdir(parents=True, exist_ok=True)
            try:
                os.rename(str(staging_dir), str(final_dir))
            except OSError:
                if not final_dir.exists():
                    raise
                existing = self._validate_existing_result(final_dir, inputs, diff_set_sha256)
                shutil.rmtree(str(staging_dir), ignore_errors=True)
                return DiffAnalysisResult(diff_set_sha256, final_dir, existing, True)
            self._fsync_directory(final_dir.parent)
            return DiffAnalysisResult(diff_set_sha256, final_dir, summary, False)
        except SingleBuildDiffAnalysisError as error:
            self._publish_failure(staging_dir, build_id, input_set_sha256, diff_set_sha256, runner, error)
            raise
        except (GitDiffParseError, CoverageExportError) as error:
            wrapped = SingleBuildDiffAnalysisError(error.code, error.message)
            self._publish_failure(staging_dir, build_id, input_set_sha256, diff_set_sha256, runner, wrapped)
            raise wrapped from error
        except Exception as error:
            wrapped = SingleBuildDiffAnalysisError(
                "diff_analysis_internal_error", "single-Build diff analysis failed unexpectedly", 5
            )
            self._publish_failure(staging_dir, build_id, input_set_sha256, diff_set_sha256, runner, wrapped)
            raise wrapped from error

    def load_and_validate_build(
        self, cycle_id: str, build_id: str
    ) -> Tuple[Dict[str, object], Dict[str, object]]:
        """Loads SQLite read-only and proves immutable Build metadata and bytes."""

        connection = self._readonly_database()
        try:
            try:
                cycle = connection.execute(
                    "SELECT id, project FROM coverage_cycles WHERE id = ?", (cycle_id,)
                ).fetchone()
            except sqlite3.OperationalError:
                cycle = connection.execute(
                    "SELECT id FROM coverage_cycles WHERE id = ?", (cycle_id,)
                ).fetchone()
            if cycle is None:
                raise SingleBuildDiffAnalysisError("cycle_not_found", "cycle_id is not registered")
            row = connection.execute("SELECT * FROM builds WHERE id = ?", (build_id,)).fetchone()
            if row is None:
                raise SingleBuildDiffAnalysisError("build_not_found", "build_id is not registered")
            build = dict(row)
            build["project"] = str(cycle["project"]) if "project" in cycle.keys() else "test-project"
        finally:
            connection.close()
        if build.get("cycle_id") != cycle_id:
            raise SingleBuildDiffAnalysisError(
                "build_cycle_mismatch", "Build does not belong to the requested Cycle"
            )
        if build.get("artifact_state") != "ready":
            raise SingleBuildDiffAnalysisError("build_not_ready", "Build artifact_state must be ready")
        coverage_binary = build.get("coverage_binary")
        if coverage_binary not in SUPPORTED_COVERAGE_BINARIES:
            raise SingleBuildDiffAnalysisError(
                "invalid_coverage_binary", "Build coverage_binary is not supported"
            )
        try:
            manifest = json.loads(str(build["manifest_json"]))
        except (KeyError, TypeError, json.JSONDecodeError) as error:
            raise SingleBuildDiffAnalysisError(
                "invalid_build_manifest", "stored Build manifest is invalid"
            ) from error
        if not isinstance(manifest, dict):
            raise SingleBuildDiffAnalysisError("invalid_build_manifest", "stored Build manifest is invalid")
        manifest_sha = self._json_sha256(manifest)
        if manifest_sha != build.get("manifest_sha256"):
            raise SingleBuildDiffAnalysisError(
                "manifest_sha256_mismatch", "stored Build manifest SHA-256 differs from SQLite"
            )
        manifest_path = self._build_directory(cycle_id, build_id) / "manifest.json"
        disk_manifest = self._load_json(manifest_path, "stored_manifest_missing")
        if disk_manifest != manifest:
            raise SingleBuildDiffAnalysisError(
                "stored_manifest_mismatch", "stored Build manifest file differs from SQLite"
            )
        for field_name in (
            "cycle_id",
            "git_sha",
            "target_sha_at_build",
            "merge_base_sha",
            "coverage_binary",
        ):
            if manifest.get(field_name) != build.get(field_name):
                raise SingleBuildDiffAnalysisError(
                    "build_manifest_mismatch", "Build manifest identity differs from SQLite"
                )
        if manifest.get("build_id") != build_id:
            raise SingleBuildDiffAnalysisError(
                "build_manifest_mismatch", "Build manifest identity differs from SQLite"
            )
        artifacts = manifest.get("artifacts")
        uuids = manifest.get("macho_uuids")
        if (
            not isinstance(artifacts, dict)
            or not isinstance(uuids, dict)
            or set(artifacts) != set(uuids)
            or not {"main_executable", "dsym"}.issubset(artifacts)
            or not set(artifacts).issubset(ARTIFACT_STORAGE_NAMES)
            or coverage_binary not in artifacts
        ):
            raise SingleBuildDiffAnalysisError(
                "invalid_artifact_set", "Build artifact and UUID sets are inconsistent"
            )
        artifact_root = self._build_directory(cycle_id, build_id) / "artifacts"
        for field_name, descriptor in artifacts.items():
            if not isinstance(descriptor, dict):
                raise SingleBuildDiffAnalysisError(
                    "invalid_build_manifest", "Build artifact descriptor is invalid"
                )
            expected_size = descriptor.get("size")
            expected_sha = descriptor.get("sha256")
            if (
                build.get(field_name + "_size") != expected_size
                or build.get(field_name + "_sha256") != expected_sha
            ):
                raise SingleBuildDiffAnalysisError(
                    "artifact_database_mismatch", field_name + " differs from SQLite"
                )
            try:
                database_uuids = json.loads(str(build[field_name + "_uuids_json"]))
            except (KeyError, TypeError, json.JSONDecodeError) as error:
                raise SingleBuildDiffAnalysisError(
                    "artifact_uuid_mismatch", field_name + " UUID metadata is invalid"
                ) from error
            if database_uuids != uuids.get(field_name):
                raise SingleBuildDiffAnalysisError(
                    "artifact_uuid_mismatch", field_name + " UUIDs differ from SQLite"
                )
            artifact_path = artifact_root / ARTIFACT_STORAGE_NAMES[field_name]
            if (
                not artifact_path.exists()
                and not artifact_path.is_symlink()
                and field_name != coverage_binary
                and self.retained_evidence_store.build_artifact_is_retained(
                    cycle_id, build_id, build.get("manifest_sha256"), field_name
                )
            ):
                continue
            size, sha256 = self._file_identity(
                artifact_path, field_name + " artifact"
            )
            if size != expected_size:
                raise SingleBuildDiffAnalysisError(
                    "artifact_size_mismatch", field_name + " size differs from manifest"
                )
            if sha256 != expected_sha:
                raise SingleBuildDiffAnalysisError(
                    "artifact_sha256_mismatch", field_name + " SHA-256 differs from manifest"
                )
        return build, manifest

    def validate_single_build_result(
        self,
        directory: Path,
        build: Mapping[str, object],
        input_set_sha256: str,
    ) -> Tuple[Dict[str, object], Dict[str, object], Path, str]:
        """Accepts only a complete immutable single-Build result matching this Build."""

        if not directory.is_dir() or directory.is_symlink():
            raise SingleBuildDiffAnalysisError(
                "single_build_result_not_found", "completed single-Build result is missing"
            )
        inputs = self._load_json(directory / "inputs.json", "single_build_result_invalid")
        summary = self._load_json(directory / "summary.json", "single_build_result_invalid")
        completion = self._load_json(directory / "completion.json", "single_build_result_invalid")
        if (
            completion.get("status") != "complete"
            or completion.get("input_set_sha256") != input_set_sha256
            or summary.get("status") != "complete"
            or summary.get("input_set_sha256") != input_set_sha256
            or inputs.get("input_set_sha256") != input_set_sha256
        ):
            raise SingleBuildDiffAnalysisError(
                "single_build_result_invalid", "single-Build completion identity is invalid"
            )
        outputs = completion.get("outputs")
        if not isinstance(outputs, dict):
            raise SingleBuildDiffAnalysisError(
                "single_build_result_invalid", "single-Build output identity is missing"
            )
        actual_names = {
            path.relative_to(directory).as_posix()
            for path in directory.rglob("*")
            if path.is_file() and path.name != "completion.json"
        }
        if actual_names != set(outputs):
            raise SingleBuildDiffAnalysisError(
                "single_build_result_invalid", "single-Build output set has changed"
            )
        for relative_name, descriptor in outputs.items():
            safe_name = self._safe_output_relative_name(relative_name)
            if not isinstance(descriptor, dict):
                raise SingleBuildDiffAnalysisError(
                    "single_build_result_invalid", "single-Build output identity is invalid"
                )
            size, sha256 = self._file_identity(directory / safe_name, "single-Build output")
            if size != descriptor.get("size") or sha256 != descriptor.get("sha256"):
                raise SingleBuildDiffAnalysisError(
                    "single_build_result_tampered", "single-Build output bytes have changed"
                )
        input_build = inputs.get("build")
        if not isinstance(input_build, dict):
            raise SingleBuildDiffAnalysisError(
                "single_build_result_invalid", "single-Build Build identity is missing"
            )
        comparisons = {
            "cycle_id": (inputs.get("cycle_id"), build.get("cycle_id")),
            "build_id": (inputs.get("build_id"), build.get("id")),
            "git_sha": (input_build.get("git_sha"), build.get("git_sha")),
            "target_sha_at_build": (
                input_build.get("target_sha_at_build"),
                build.get("target_sha_at_build"),
            ),
            "merge_base_sha": (input_build.get("merge_base_sha"), build.get("merge_base_sha")),
            "manifest_sha256": (
                input_build.get("manifest_sha256"),
                build.get("manifest_sha256"),
            ),
        }
        for field_name in (
            "cycle_id",
            "build_id",
            "git_sha",
            "target_sha_at_build",
            "merge_base_sha",
            "manifest_sha256",
        ):
            summary_value = summary.get(field_name)
            expected = build.get("id") if field_name == "build_id" else build.get(field_name)
            if field_name == "cycle_id":
                expected = build.get("cycle_id")
            if comparisons[field_name][0] != comparisons[field_name][1] or summary_value != expected:
                raise SingleBuildDiffAnalysisError(
                    "single_build_identity_mismatch",
                    "single-Build result differs from current Build: " + field_name,
                )
        export_path = directory / "llvm-cov-export.json"
        export_size, export_sha = self._file_identity(export_path, "llvm-cov export")
        descriptor = outputs.get("llvm-cov-export.json")
        if not isinstance(descriptor, dict) or descriptor.get("size") != export_size or descriptor.get(
            "sha256"
        ) != export_sha:
            raise SingleBuildDiffAnalysisError(
                "single_build_result_tampered", "llvm-cov export identity is invalid"
            )
        return inputs, summary, export_path, export_sha

    def _validate_git_snapshot(self, runner: object, build: Mapping[str, object], repository: Path) -> None:
        """Records the fixed repository root, commit objects, and exact merge-base."""

        repository = repository.resolve()
        if not repository.is_dir():
            raise SingleBuildDiffAnalysisError(
                "git_repository_invalid", "configured project repository is unavailable"
            )
        top_level = self._run_git(
            runner,
            "git-repository-root",
            ["git", "-C", str(repository), "rev-parse", "--show-toplevel"],
        ).stdout.strip()
        if not top_level or Path(top_level).resolve() != repository:
            raise SingleBuildDiffAnalysisError(
                "git_repository_invalid", "configured project repository is not the Git worktree root"
            )
        for index, field_name in enumerate(
            ("git_sha", "target_sha_at_build", "merge_base_sha"), start=1
        ):
            value = str(build[field_name])
            if not GIT_SHA_PATTERN.fullmatch(value):
                raise SingleBuildDiffAnalysisError("invalid_git_sha", field_name + " is not a full SHA")
            self._run_git(
                runner,
                "git-cat-file-{:02d}".format(index),
                ["git", "-C", str(repository), "cat-file", "-e", value + "^{commit}"],
            )
        merge = self._run_git(
            runner,
            "git-merge-base",
            [
                "git",
                "-C",
                str(repository),
                "merge-base",
                str(build["target_sha_at_build"]),
                str(build["git_sha"]),
            ],
        ).stdout.strip().lower()
        if merge != build["merge_base_sha"]:
            raise SingleBuildDiffAnalysisError(
                "merge_base_mismatch", "fixed Build merge_base_sha no longer validates"
            )

    def _generate_git_diff(
        self, runner: object, build: Mapping[str, object], repository_path: Path
    ) -> List[NormalizedDiffFile]:
        """Records rename-aware status and a zero-context fixed-SHA patch."""

        repository = str(repository_path.resolve())
        base = str(build["merge_base_sha"])
        head = str(build["git_sha"])
        status_result = self._run_git(
            runner,
            "git-name-status",
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
                base,
                head,
                "--",
            ],
        )
        statuses = parse_name_status_z(status_result.stdout)
        self._run_git(
            runner,
            "git-diff",
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
                base,
                head,
                "--",
            ],
            stdout_filename="git-diff.patch",
        )
        patch_path = runner.evidence_dir / "git-diff.patch"
        try:
            patch = patch_path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as error:
            raise SingleBuildDiffAnalysisError(
                "git_diff_invalid", "Git patch is not readable UTF-8"
            ) from error
        return parse_unified_zero_patch(patch, statuses)

    def _classify_changed_lines(
        self,
        runner: object,
        files: Sequence[NormalizedDiffFile],
        coverage: NormalizedCoverage,
        git_sha: str,
        repository_path: Path,
    ) -> Tuple[List[Dict[str, object]], List[Dict[str, object]]]:
        """Loads new-side Git objects and applies conservative compiler-region states."""

        changed_lines: List[Dict[str, object]] = []
        git_files: List[Dict[str, object]] = []
        repository = str(repository_path.resolve())
        source_index = 0
        for item in files:
            file_record: Dict[str, object] = {
                "git_status": item.status,
                "old_path": item.old_path,
                "new_path": item.new_path,
                "similarity": item.similarity,
                "binary": item.binary,
                "changed_new_line_count": len(item.changed_new_lines),
                "deleted_excluded": item.deleted_excluded,
            }
            if item.new_path is None or item.binary or not item.changed_new_lines:
                file_record["source_command"] = None
                git_files.append(file_record)
                continue
            source_index += 1
            label = "git-show-{:04d}".format(source_index)
            result = self._run_git(
                runner,
                label,
                ["git", "-C", repository, "show", git_sha + ":" + item.new_path],
            )
            source_lines = result.stdout.splitlines()
            file_record["source_command"] = label
            file_record["source_size"] = len(result.stdout.encode("utf-8"))
            file_record["source_sha256"] = hashlib.sha256(
                result.stdout.encode("utf-8")
            ).hexdigest()
            for line_number in item.changed_new_lines:
                state, reason, related = self._line_state(
                    item.new_path, line_number, source_lines, coverage
                )
                source_text = source_lines[line_number - 1] if 0 < line_number <= len(source_lines) else ""
                changed_lines.append(
                    {
                        "repo_relative_path": item.new_path,
                        "git_status": item.status,
                        "old_path": item.old_path,
                        "new_path": item.new_path,
                        "new_line_number": line_number,
                        "source_text": source_text,
                        "source_text_sha256": hashlib.sha256(
                            source_text.encode("utf-8")
                        ).hexdigest(),
                        "state": state,
                        "reason": reason,
                        "measurable_regions": [region.as_dict() for region in related],
                    }
                )
            git_files.append(file_record)
        changed_lines.sort(key=lambda value: (str(value["repo_relative_path"]), int(value["new_line_number"])))
        return changed_lines, git_files

    def _line_state(
        self,
        path: str,
        line_number: int,
        source_lines: Sequence[str],
        coverage: NormalizedCoverage,
    ) -> Tuple[str, str, Sequence[object]]:
        """Classifies exactly one Git line using all intersecting code regions."""

        if path in coverage.conflicted_paths:
            return "unknown", coverage.conflicted_paths[path], ()
        regions = coverage.regions_by_path.get(path)
        if regions is None:
            if line_number <= 0 or line_number > len(source_lines):
                return "unknown", "git_changed_line_out_of_range", ()
            return "not_measurable", "no_compiler_coverage_mapping_for_path", ()
        related, error = regions_for_source_line(regions, line_number, source_lines)
        if error:
            return "unknown", error, ()
        if not related:
            return "not_measurable", "no_measurable_non_gap_region", ()
        if any(region.count == 0 for region in related):
            reason = "at_least_one_related_region_has_zero_count"
            if any(region.count > 0 for region in related):
                reason = "mixed_region_counts_conservatively_uncovered"
            return "uncovered", reason, related
        return "covered", "all_related_regions_have_positive_count", related

    def _build_summary(
        self,
        build: Mapping[str, object],
        input_set_sha256: str,
        diff_set_sha256: str,
        export_sha256: str,
        normalized_diff_sha256: str,
        files: Sequence[NormalizedDiffFile],
        lines: Sequence[Mapping[str, object]],
        git_files: Sequence[Mapping[str, object]],
        started_at: str,
        completed_at: str,
        coverage: NormalizedCoverage,
    ) -> Dict[str, object]:
        """Builds overall and per-file changed-line totals with an explicit denominator."""

        state_names = ("covered", "uncovered", "not_measurable", "unknown")
        totals = {name: sum(1 for line in lines if line["state"] == name) for name in state_names}
        measurable = totals["covered"] + totals["uncovered"]
        percent: Optional[float] = None
        if measurable:
            percent = totals["covered"] / measurable * 100.0
        per_file: List[Dict[str, object]] = []
        with_measurable = 0
        only_not_measurable = 0
        with_unknown = 0
        for file_item, git_file in zip(files, git_files):
            path = file_item.new_path or file_item.old_path
            file_lines = [line for line in lines if line["repo_relative_path"] == file_item.new_path]
            counts = {name: sum(1 for line in file_lines if line["state"] == name) for name in state_names}
            file_measurable = counts["covered"] + counts["uncovered"]
            if file_measurable:
                with_measurable += 1
            if counts["unknown"]:
                with_unknown += 1
            if file_item.binary or (
                file_lines
                and counts["not_measurable"] == len(file_lines)
            ):
                only_not_measurable += 1
            per_file.append(
                {
                    "repo_relative_path": path,
                    "git_status": file_item.status,
                    "binary": file_item.binary,
                    "changed_new_side": len(file_lines),
                    "measurable": file_measurable,
                    **counts,
                    "deleted_excluded": file_item.deleted_excluded,
                }
            )
        warnings = []
        if coverage.unmapped_files:
            warnings.append(
                {
                    "code": "coverage_files_outside_fixed_repository_ignored",
                    "count": len(coverage.unmapped_files),
                }
            )
        return {
            "schema_version": DIFF_ANALYSIS_SCHEMA_VERSION,
            "status": "complete",
            "cycle_id": build["cycle_id"],
            "build_id": build["id"],
            "input_set_sha256": input_set_sha256,
            "diff_set_sha256": diff_set_sha256,
            "git_sha": build["git_sha"],
            "target_sha_at_build": build["target_sha_at_build"],
            "merge_base_sha": build["merge_base_sha"],
            "manifest_sha256": build["manifest_sha256"],
            "llvm_cov_export_sha256": export_sha256,
            "normalized_git_diff_sha256": normalized_diff_sha256,
            "files": {
                "changed": len(files),
                "with_measurable_changes": with_measurable,
                "only_not_measurable": only_not_measurable,
                "with_unknown": with_unknown,
            },
            "lines": {
                "changed_new_side": len(lines),
                "measurable": measurable,
                **totals,
                "deleted_excluded": sum(item.deleted_excluded for item in files),
            },
            "changed_line_coverage_percent": percent,
            "per_file": per_file,
            "started_at": started_at,
            "completed_at": completed_at,
            "warnings": warnings,
            "granularity": (
                "Git new-side lines intersected with compiler coverage regions; line status is not "
                "Token-level semantic test proof, and count>0 means only that a region executed."
            ),
        }

    def _run_git(
        self,
        runner: object,
        label: str,
        arguments: Sequence[str],
        stdout_filename: Optional[str] = None,
    ) -> object:
        """Runs one fixed read-only Git command and rejects all stderr/nonzero output."""

        result = runner.run(label, arguments, stdout_filename=stdout_filename)
        if result.returncode != 0:
            raise SingleBuildDiffAnalysisError(
                "git_command_failed", label + " exited with code " + str(result.returncode), 4
            )
        if result.stderr:
            raise SingleBuildDiffAnalysisError("git_stderr_not_empty", label + " emitted stderr", 4)
        return result

    def _validate_existing_result(
        self, directory: Path, inputs: Mapping[str, object], diff_set_sha256: str
    ) -> Mapping[str, object]:
        """Reuses only an exact complete result whose bytes remain unchanged."""

        stored_inputs = self._load_json(directory / "inputs.json", "existing_diff_result_invalid")
        summary = self._load_json(directory / "summary.json", "existing_diff_result_invalid")
        completion = self._load_json(
            directory / "completion.json", "existing_diff_result_invalid"
        )
        if stored_inputs != inputs:
            raise SingleBuildDiffAnalysisError(
                "existing_diff_result_conflict", "existing diff result has different inputs", 5
            )
        if (
            completion.get("status") != "complete"
            or completion.get("diff_set_sha256") != diff_set_sha256
            or summary.get("status") != "complete"
            or summary.get("diff_set_sha256") != diff_set_sha256
        ):
            raise SingleBuildDiffAnalysisError(
                "existing_diff_result_invalid", "existing diff completion is invalid", 5
            )
        outputs = completion.get("outputs")
        if not isinstance(outputs, dict):
            raise SingleBuildDiffAnalysisError(
                "existing_diff_result_invalid", "existing diff output identity is missing", 5
            )
        actual_names = {
            path.relative_to(directory).as_posix()
            for path in directory.rglob("*")
            if path.is_file() and path.name != "completion.json"
        }
        if actual_names != set(outputs):
            raise SingleBuildDiffAnalysisError(
                "existing_diff_result_invalid", "existing diff output set has changed", 5
            )
        for relative_name, descriptor in outputs.items():
            safe_name = self._safe_output_relative_name(relative_name)
            if not isinstance(descriptor, dict):
                raise SingleBuildDiffAnalysisError(
                    "existing_diff_result_invalid", "existing diff output identity is invalid", 5
                )
            size, sha256 = self._file_identity(directory / safe_name, "diff analysis output")
            if size != descriptor.get("size") or sha256 != descriptor.get("sha256"):
                raise SingleBuildDiffAnalysisError(
                    "existing_diff_result_invalid", "existing diff output bytes have changed", 5
                )
        return summary

    def _publish_failure(
        self,
        staging_dir: Path,
        build_id: str,
        input_set_sha256: str,
        diff_set_sha256: Optional[str],
        runner: object,
        error: SingleBuildDiffAnalysisError,
    ) -> None:
        """Moves failed evidence outside the successful hash namespace."""

        if not staging_dir.exists():
            return
        try:
            commands_path = staging_dir / "commands.json"
            if not commands_path.exists():
                self._write_json(commands_path, {"commands": runner.records})
            self._write_json(
                staging_dir / "failure.json",
                {
                    "schema_version": DIFF_ANALYSIS_SCHEMA_VERSION,
                    "status": "failed",
                    "build_id": build_id,
                    "input_set_sha256": input_set_sha256,
                    "diff_set_sha256": diff_set_sha256,
                    "error": error.code,
                    "message": error.message,
                    "failed_at": self._utc_now(),
                },
            )
            self._fsync_directory(staging_dir)
            parent = self.diff_analysis_root / ".failed" / build_id / input_set_sha256
            parent.mkdir(parents=True, exist_ok=True)
            destination = parent / (self._utc_compact() + "-" + uuid.uuid4().hex)
            os.rename(str(staging_dir), str(destination))
            self._fsync_directory(parent)
        except Exception:
            shutil.rmtree(str(staging_dir), ignore_errors=True)

    def _readonly_database(self) -> sqlite3.Connection:
        """Opens the service database in read-only query-only mode."""

        database = self.configuration.database_path.resolve()
        if not database.is_file():
            raise SingleBuildDiffAnalysisError("database_not_found", "coverage SQLite database is missing")
        uri = "file:" + quote(str(database), safe="/") + "?mode=ro"
        connection = sqlite3.connect(uri, uri=True, timeout=10, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only = ON")
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    def _single_build_directory(self, build_id: str, input_set_sha256: str) -> Path:
        """Resolves one prior result beneath the fixed analysis root."""

        return self.configuration.analysis_root / build_id / input_set_sha256

    def _final_directory(self, build_id: str, input_sha: str, diff_sha: str) -> Path:
        """Resolves one immutable successful changed-line output directory."""

        return self.diff_analysis_root / build_id / input_sha / diff_sha

    def _make_staging_directory(self, build_id: str) -> Path:
        """Creates an unpublished same-filesystem staging directory."""

        root = self.diff_analysis_root / ".staging"
        root.mkdir(parents=True, exist_ok=True)
        return Path(tempfile.mkdtemp(prefix="." + build_id + "-", dir=str(root)))

    def _build_directory(self, cycle_id: str, build_id: str) -> Path:
        """Returns one server-owned immutable Build directory."""

        return self.configuration.data_dir / "cycles" / cycle_id / "builds" / build_id

    def _load_json(self, path: Path, code: str) -> Dict[str, object]:
        """Loads a required JSON object with one stable error code."""

        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
            raise SingleBuildDiffAnalysisError(code, path.name + " is missing or invalid") from error
        if not isinstance(payload, dict):
            raise SingleBuildDiffAnalysisError(code, path.name + " must contain a JSON object")
        return payload

    def _file_identity(self, path: Path, label: str) -> Tuple[int, str]:
        """Hashes one required regular non-symlink file."""

        if not path.is_file() or path.is_symlink():
            raise SingleBuildDiffAnalysisError("stored_file_missing", label + " is missing")
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
            raise SingleBuildDiffAnalysisError("stored_file_unreadable", label + " cannot be read") from error
        return size, digest.hexdigest()

    def _directory_file_identity(self, directory: Path) -> Dict[str, Dict[str, object]]:
        """Hashes every staged output before creating completion.json."""

        result: Dict[str, Dict[str, object]] = {}
        for path in sorted(candidate for candidate in directory.rglob("*") if candidate.is_file()):
            relative = path.relative_to(directory).as_posix()
            size, sha256 = self._file_identity(path, "diff analysis output")
            result[relative] = {"size": size, "sha256": sha256}
        return result

    def _safe_output_relative_name(self, value: object) -> str:
        """Rejects completion entries capable of escaping an evidence directory."""

        if not isinstance(value, str) or not value:
            raise SingleBuildDiffAnalysisError(
                "result_output_path_invalid", "analysis output path is invalid", 5
            )
        path = PurePosixPath(value)
        if path.is_absolute() or path.as_posix() != value or ".." in path.parts:
            raise SingleBuildDiffAnalysisError(
                "result_output_path_invalid", "analysis output path is unsafe", 5
            )
        return value

    def _write_json(self, destination: Path, payload: object) -> None:
        """Writes deterministic JSON and fsyncs it before publication."""

        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2).encode(
            "utf-8"
        ) + b"\n"
        with destination.open("xb") as output:
            output.write(encoded)
            output.flush()
            os.fsync(output.fileno())

    def _fsync_directory(self, directory: Path) -> None:
        """Flushes one directory entry before or after atomic rename."""

        descriptor = os.open(str(directory), os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def _normalize_uuid(self, value: str, field_name: str) -> str:
        """Normalizes one CLI UUID before it can affect a filesystem path."""

        try:
            return str(uuid.UUID(value))
        except (AttributeError, ValueError) as error:
            raise SingleBuildDiffAnalysisError("invalid_uuid", field_name + " must be a UUID") from error

    def _normalize_sha256(self, value: str, field_name: str) -> str:
        """Accepts one lowercase full SHA-256 identity from the CLI."""

        if not isinstance(value, str) or not SHA256_PATTERN.fullmatch(value):
            raise SingleBuildDiffAnalysisError("invalid_sha256", field_name + " must be lowercase SHA-256")
        return value

    def _json_sha256(self, payload: object) -> str:
        """Hashes canonical semantic JSON."""

        return hashlib.sha256(
            json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
                "utf-8"
            )
        ).hexdigest()

    def _utc_now(self) -> str:
        """Returns a timezone-explicit UTC timestamp."""

        return datetime.now(timezone.utc).isoformat()

    def _utc_compact(self) -> str:
        """Returns a filesystem-safe UTC failure timestamp."""

        return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
