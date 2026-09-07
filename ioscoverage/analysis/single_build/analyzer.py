#!/usr/bin/env python3
"""Restricted, repeatable local LLVM analysis for one registered iOS Build."""

import hashlib
import json
import os
import shutil
import sqlite3
import tempfile
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Dict, List, Mapping, Optional, Sequence, Tuple
from urllib.parse import quote

from ioscoverage.analysis.command_runner import EvidenceCommandRunner
from ioscoverage.integrations.xcode_artifacts import (
    DEFAULT_DEVELOPER_DIR,
    ArtifactValidationError,
    XcodeArtifactValidator,
)
from ioscoverage.storage.coverage_store import ARTIFACT_STORAGE_NAMES
from ioscoverage.integrations.git_repository import GitRepository, GitRepositoryError
from ioscoverage.projects import ProjectRegistry, ProjectRegistryError
from ioscoverage.runtime import RuntimePaths


ANALYSIS_SCHEMA_VERSION = 1
MAX_DSYM_UNCOMPRESSED_BYTES = 512 * 1024 * 1024
SUPPORTED_COVERAGE_BINARIES = ("main_executable", "debug_dylib")


class SingleBuildAnalysisError(Exception):
    """Carries a stable CLI error code, message, and process exit category."""

    def __init__(self, code: str, message: str, exit_code: int = 3):
        """Creates one safe analysis error without printing local credentials."""

        super().__init__(message)
        self.code = code
        self.message = message
        self.exit_code = exit_code


@dataclass(frozen=True)
class AnalysisConfiguration:
    """Owns fixed local paths that are not configurable through the CLI."""

    database_path: Path
    data_dir: Path
    analysis_root: Path
    developer_dir: Path = DEFAULT_DEVELOPER_DIR
    project_registry: Optional[ProjectRegistry] = None
    repository_path: Optional[Path] = None

    @classmethod
    def for_runtime_root(cls, runtime_root: Path) -> "AnalysisConfiguration":
        """Builds production analysis paths and registry from one runtime root."""

        paths = RuntimePaths.from_root(runtime_root)
        return cls(
            database_path=paths.database,
            data_dir=paths.data_dir,
            analysis_root=paths.analysis_dir / "single-build",
            project_registry=ProjectRegistry.load(paths.projects_file),
        )

    def project_repository(self, project: object) -> GitRepository:
        """Resolves a Cycle project without consulting HEAD, cwd, or source layout."""

        if self.project_registry is not None:
            try:
                return self.project_registry.repository(project)
            except ProjectRegistryError as error:
                raise SingleBuildAnalysisError(error.code, error.message) from error
        if self.repository_path is not None:
            return GitRepository(self.repository_path)
        raise SingleBuildAnalysisError("project_not_registered", "Cycle project is not registered")

    def project_repository_path(self, project: object) -> Path:
        """Returns the trusted worktree root for one Cycle project."""

        if self.project_registry is not None:
            return self.project_repository(project).path
        if self.repository_path is not None:
            return self.repository_path.resolve()
        raise SingleBuildAnalysisError("project_not_registered", "Cycle project is not registered")


@dataclass(frozen=True)
class AnalysisResult:
    """Returns the immutable published result and whether it was reused."""

    input_set_sha256: str
    output_directory: Path
    summary: Mapping[str, object]
    idempotent_replay: bool


class SingleBuildAnalyzer:
    """Validates one exact Build/Run set and atomically publishes LLVM evidence."""

    def __init__(
        self,
        configuration: AnalysisConfiguration,
        repository: Optional[object] = None,
        command_runner_factory: Callable[[Path, Path], object] = EvidenceCommandRunner,
        artifact_validator_factory: Optional[Callable[[Path, Callable], object]] = None,
    ):
        """Injects controllable process dependencies while retaining fixed production paths."""

        self.configuration = configuration
        self.repository_override = repository
        self.command_runner_factory = command_runner_factory
        self.artifact_validator_factory = artifact_validator_factory or self._artifact_validator

    def analyze(
        self, cycle_id: str, build_id: str, run_ids: Sequence[str]
    ) -> AnalysisResult:
        """Runs or reuses one complete single-Build analysis transaction."""

        started_at = self._utc_now()
        cycle_id = self._normalize_uuid(cycle_id, "cycle_id")
        build_id = self._normalize_uuid(build_id, "build_id")
        normalized_run_ids = [self._normalize_uuid(value, "run_id") for value in run_ids]
        if not normalized_run_ids:
            raise SingleBuildAnalysisError("run_id_required", "at least one run_id is required")
        if len(set(normalized_run_ids)) != len(normalized_run_ids):
            raise SingleBuildAnalysisError("duplicate_run_id", "run_id values must be unique")
        normalized_run_ids.sort()

        staging_dir = self._make_staging_directory(build_id)
        runner = self.command_runner_factory(staging_dir, self.configuration.developer_dir)
        input_set_sha256: Optional[str] = None
        try:
            build, manifest, artifact_paths, runs = self._load_and_validate_inputs(
                cycle_id, build_id, normalized_run_ids
            )
            self._validate_git(build)
            validator = self.artifact_validator_factory(
                self.configuration.developer_dir, runner.run_for_artifact_validator
            )
            try:
                actual_uuids = validator.validate(
                    artifact_paths,
                    manifest["macho_uuids"],
                    str(build["coverage_binary"]),
                    staging_dir,
                    MAX_DSYM_UNCOMPRESSED_BYTES,
                )
            except ArtifactValidationError as error:
                exit_code = 4 if error.code.startswith(("artifact_tool", "xcode_toolchain")) else 3
                raise SingleBuildAnalysisError(error.code, error.message, exit_code) from error

            inputs = self._build_inputs_document(build, manifest, runs, actual_uuids)
            input_set_sha256 = self._input_set_sha256(inputs)
            inputs["input_set_sha256"] = input_set_sha256
            final_dir = (
                self.configuration.analysis_root / build_id / input_set_sha256
            ).resolve()
            if final_dir.exists():
                summary = self._validate_existing_result(final_dir, inputs, input_set_sha256)
                shutil.rmtree(str(staging_dir), ignore_errors=True)
                return AnalysisResult(input_set_sha256, final_dir, summary, True)

            self._write_json(staging_dir / "inputs.json", inputs)
            tool_versions = self._collect_tool_versions(runner)
            self._write_json(staging_dir / "tool_versions.json", tool_versions)
            self._show_each_profile(runner, runs)
            merged_path = staging_dir / "merged.profdata"
            self._merge_profiles(runner, runs, merged_path)
            self._show_merged_profile(runner, merged_path)
            export_path = staging_dir / "llvm-cov-export.json"
            self._export_coverage(
                runner,
                artifact_paths[str(build["coverage_binary"])],
                merged_path,
                export_path,
            )
            export_payload = self._load_export(export_path)
            completed_at = self._utc_now()
            summary = self._build_summary(
                build,
                runs,
                input_set_sha256,
                merged_path,
                tool_versions,
                export_payload,
                started_at,
                completed_at,
            )
            self._write_json(staging_dir / "summary.json", summary)
            self._write_json(staging_dir / "commands.json", {"commands": runner.records})
            output_identity = self._directory_file_identity(staging_dir)
            completion = {
                "schema_version": ANALYSIS_SCHEMA_VERSION,
                "status": "complete",
                "input_set_sha256": input_set_sha256,
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
                existing = self._validate_existing_result(final_dir, inputs, input_set_sha256)
                shutil.rmtree(str(staging_dir), ignore_errors=True)
                return AnalysisResult(input_set_sha256, final_dir, existing, True)
            self._fsync_directory(final_dir.parent)
            return AnalysisResult(input_set_sha256, final_dir, summary, False)
        except SingleBuildAnalysisError as error:
            self._publish_failure(staging_dir, build_id, input_set_sha256, runner, error)
            raise
        except Exception as error:
            wrapped = SingleBuildAnalysisError(
                "analysis_internal_error", "single-Build analysis failed unexpectedly", 5
            )
            self._publish_failure(staging_dir, build_id, input_set_sha256, runner, wrapped)
            raise wrapped from error

    def _load_and_validate_inputs(
        self, cycle_id: str, build_id: str, run_ids: Sequence[str]
    ) -> Tuple[Dict[str, object], Dict[str, object], Dict[str, Path], List[Dict[str, object]]]:
        """Loads SQLite read-only and validates every immutable input byte."""

        connection = self._readonly_database()
        try:
            cycle = connection.execute(
                "SELECT * FROM coverage_cycles WHERE id = ?", (cycle_id,)
            ).fetchone()
            if cycle is None:
                raise SingleBuildAnalysisError("cycle_not_found", "cycle_id is not registered")
            build_row = connection.execute(
                "SELECT * FROM builds WHERE id = ?", (build_id,)
            ).fetchone()
            if build_row is None:
                raise SingleBuildAnalysisError("build_not_found", "build_id is not registered")
            build = dict(build_row)
            build["project"] = str(cycle["project"]) if "project" in cycle.keys() else "test-project"
            if build["cycle_id"] != cycle_id:
                raise SingleBuildAnalysisError(
                    "build_cycle_mismatch", "Build does not belong to the requested Cycle"
                )
            if build["artifact_state"] != "ready":
                raise SingleBuildAnalysisError(
                    "build_not_ready", "Build artifact_state must be ready"
                )
            manifest = self._validate_build_files(build, cycle_id, build_id)
            artifact_paths = self._artifact_paths(manifest, cycle_id, build_id)
            runs: List[Dict[str, object]] = []
            for run_id in run_ids:
                row = connection.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
                if row is None:
                    raise SingleBuildAnalysisError("run_not_found", "run_id is not stored: " + run_id)
                run = dict(row)
                self._validate_run(run, cycle_id, build_id)
                runs.append(run)
            return build, manifest, artifact_paths, runs
        finally:
            connection.close()

    def _validate_build_files(
        self, build: Mapping[str, object], cycle_id: str, build_id: str
    ) -> Dict[str, object]:
        """Proves manifest, SQLite descriptors, and all stored artifacts agree."""

        coverage_binary = build.get("coverage_binary")
        if coverage_binary not in SUPPORTED_COVERAGE_BINARIES:
            raise SingleBuildAnalysisError(
                "invalid_coverage_binary", "Build coverage_binary is not supported"
            )
        try:
            database_manifest = json.loads(str(build["manifest_json"]))
        except (KeyError, TypeError, json.JSONDecodeError) as error:
            raise SingleBuildAnalysisError("invalid_build_manifest", "stored Build manifest is invalid") from error
        if not isinstance(database_manifest, dict):
            raise SingleBuildAnalysisError("invalid_build_manifest", "stored Build manifest is invalid")
        canonical = self._canonical_json_bytes(database_manifest)
        if hashlib.sha256(canonical).hexdigest() != build.get("manifest_sha256"):
            raise SingleBuildAnalysisError(
                "manifest_sha256_mismatch", "stored Build manifest SHA-256 does not match SQLite"
            )
        manifest_path = self._build_directory(cycle_id, build_id) / "manifest.json"
        try:
            disk_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise SingleBuildAnalysisError(
                "stored_manifest_missing", "stored Build manifest file is missing or invalid"
            ) from error
        if disk_manifest != database_manifest:
            raise SingleBuildAnalysisError(
                "stored_manifest_mismatch", "stored Build manifest file differs from SQLite"
            )
        identity_fields = (
            "build_id",
            "cycle_id",
            "git_sha",
            "target_sha_at_build",
            "merge_base_sha",
            "coverage_binary",
        )
        for field_name in identity_fields:
            manifest_name = "build_id" if field_name == "build_id" else field_name
            expected = build["id"] if field_name == "build_id" else build[field_name]
            if database_manifest.get(manifest_name) != expected:
                raise SingleBuildAnalysisError(
                    "build_manifest_mismatch", "Build manifest identity differs from SQLite"
                )
        artifacts = database_manifest.get("artifacts")
        declared_uuids = database_manifest.get("macho_uuids")
        if not isinstance(artifacts, dict) or not isinstance(declared_uuids, dict):
            raise SingleBuildAnalysisError(
                "invalid_build_manifest", "Build artifacts and macho_uuids must be objects"
            )
        if set(artifacts) != set(declared_uuids) or not {"main_executable", "dsym"}.issubset(artifacts):
            raise SingleBuildAnalysisError(
                "invalid_artifact_set", "Build manifest artifact set is invalid"
            )
        if coverage_binary not in artifacts:
            raise SingleBuildAnalysisError(
                "coverage_binary_missing", "Build coverage_binary artifact is not declared"
            )
        unknown = set(artifacts) - set(ARTIFACT_STORAGE_NAMES)
        if unknown:
            raise SingleBuildAnalysisError("invalid_artifact_set", "Build has an unknown artifact")
        artifact_paths = self._artifact_paths(database_manifest, cycle_id, build_id)
        for field_name, descriptor_value in artifacts.items():
            if not isinstance(descriptor_value, dict):
                raise SingleBuildAnalysisError("invalid_build_manifest", "artifact descriptor is invalid")
            descriptor = descriptor_value
            expected_sha = descriptor.get("sha256")
            expected_size = descriptor.get("size")
            database_prefix = field_name
            if build.get(database_prefix + "_sha256") != expected_sha or build.get(
                database_prefix + "_size"
            ) != expected_size:
                raise SingleBuildAnalysisError(
                    "artifact_database_mismatch", field_name + " descriptor differs from SQLite"
                )
            path = artifact_paths[field_name]
            actual_size, actual_sha = self._file_identity(path, field_name + " artifact")
            if actual_size != expected_size:
                raise SingleBuildAnalysisError(
                    "artifact_size_mismatch", field_name + " size differs from its manifest"
                )
            if actual_sha != expected_sha:
                raise SingleBuildAnalysisError(
                    "artifact_sha256_mismatch", field_name + " SHA-256 differs from its manifest"
                )
            uuid_column = field_name + "_uuids_json"
            try:
                database_uuids = json.loads(str(build[uuid_column]))
            except (KeyError, TypeError, json.JSONDecodeError) as error:
                raise SingleBuildAnalysisError(
                    "artifact_uuid_mismatch", field_name + " UUID metadata is invalid"
                ) from error
            if database_uuids != declared_uuids.get(field_name):
                raise SingleBuildAnalysisError(
                    "artifact_uuid_mismatch", field_name + " UUIDs differ from SQLite"
                )
        return database_manifest

    def _validate_run(
        self, run: Dict[str, object], cycle_id: str, build_id: str
    ) -> None:
        """Proves one Run identity, state, metadata, size, and profile hash."""

        run_id = str(run["id"])
        if run["cycle_id"] != cycle_id:
            raise SingleBuildAnalysisError(
                "run_cycle_mismatch", "Run does not belong to the requested Cycle: " + run_id
            )
        if run["build_id"] != build_id:
            raise SingleBuildAnalysisError(
                "run_build_mismatch", "Run does not belong to the requested Build: " + run_id
            )
        if run["state"] != "uploaded" or int(run["analyzable"]) != 1:
            raise SingleBuildAnalysisError(
                "run_not_analyzable", "Run must be uploaded and analyzable: " + run_id
            )
        if run.get("error_message") not in (None, ""):
            raise SingleBuildAnalysisError(
                "run_has_error", "Run error_message must be empty: " + run_id
            )
        run_directory = self._run_directory(cycle_id, run_id)
        metadata_path = run_directory / "metadata.json"
        profile_path = run_directory / "payload.profraw"
        upload_sha_path = run_directory / "upload.sha256"
        try:
            disk_metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            database_metadata = json.loads(str(run["metadata_json"]))
        except (OSError, json.JSONDecodeError) as error:
            raise SingleBuildAnalysisError(
                "run_metadata_invalid", "Run metadata is missing or invalid: " + run_id
            ) from error
        if disk_metadata != database_metadata:
            raise SingleBuildAnalysisError(
                "run_metadata_mismatch", "Run metadata differs from SQLite: " + run_id
            )
        for field_name, expected in (
            ("run_id", run_id),
            ("cycle_id", cycle_id),
            ("build_id", build_id),
        ):
            if disk_metadata.get(field_name) != expected:
                raise SingleBuildAnalysisError(
                    "run_metadata_mismatch", "Run metadata identity is inconsistent: " + run_id
                )
        metadata_sha = hashlib.sha256(self._canonical_json_bytes(database_metadata)).hexdigest()
        if metadata_sha != run["metadata_sha256"]:
            raise SingleBuildAnalysisError(
                "run_metadata_sha256_mismatch", "Run metadata SHA-256 differs from SQLite: " + run_id
            )
        actual_size, actual_sha = self._file_identity(profile_path, "Run profraw")
        if actual_size != run["payload_size"]:
            raise SingleBuildAnalysisError(
                "profraw_size_mismatch", "Run profraw size differs from SQLite: " + run_id
            )
        if actual_sha != run["payload_sha256"]:
            raise SingleBuildAnalysisError(
                "profraw_sha256_mismatch", "Run profraw SHA-256 differs from SQLite: " + run_id
            )
        try:
            uploaded_sha = upload_sha_path.read_text(encoding="ascii").strip()
        except OSError as error:
            raise SingleBuildAnalysisError(
                "run_sha_file_missing", "Run upload.sha256 is missing: " + run_id
            ) from error
        if uploaded_sha != actual_sha:
            raise SingleBuildAnalysisError(
                "run_sha_file_mismatch", "Run upload.sha256 differs from payload: " + run_id
            )
        run["profile_path"] = profile_path

    def _validate_git(self, build: Mapping[str, object]) -> None:
        """Validates only immutable Build commit objects and their merge-base."""

        try:
            repository = self.repository_override or self.configuration.project_repository(build["project"])
            repository.validate_build_snapshot(
                str(build["git_sha"]),
                str(build["target_sha_at_build"]),
                str(build["merge_base_sha"]),
            )
        except GitRepositoryError as error:
            raise SingleBuildAnalysisError(error.code, error.message) from error

    def _collect_tool_versions(self, runner: object) -> Dict[str, object]:
        """Records the fixed Xcode and LLVM versions used by this analysis."""

        xcode = self._run_required(runner, "tool-version-xcode", ["xcodebuild", "-version"])
        profdata = self._run_required(
            runner, "tool-version-llvm-profdata", ["xcrun", "llvm-profdata", "--version"]
        )
        cov = self._run_required(
            runner, "tool-version-llvm-cov", ["xcrun", "llvm-cov", "--version"]
        )
        if not xcode.stdout.startswith("Xcode 26.3\n"):
            raise SingleBuildAnalysisError(
                "xcode_toolchain_invalid", "configured developer directory is not Xcode 26.3", 4
            )
        return {
            "developer_dir": str(self.configuration.developer_dir.resolve()),
            "xcode": xcode.stdout.strip(),
            "llvm_profdata": profdata.stdout.strip(),
            "llvm_cov": cov.stdout.strip(),
        }

    def _show_each_profile(self, runner: object, runs: Sequence[Mapping[str, object]]) -> None:
        """Requires every raw profile to be independently readable before merge."""

        for index, run in enumerate(runs, start=1):
            self._run_required(
                runner,
                "llvm-profdata-show-run-{:02d}".format(index),
                ["xcrun", "llvm-profdata", "show", "--binary-ids", str(run["profile_path"])],
            )

    def _merge_profiles(
        self, runner: object, runs: Sequence[Mapping[str, object]], merged_path: Path
    ) -> None:
        """Merges only the explicitly selected same-Build profiles."""

        arguments = [
            "xcrun",
            "llvm-profdata",
            "merge",
            "-sparse",
            "-o",
            str(merged_path),
        ]
        arguments.extend(str(run["profile_path"]) for run in runs)
        self._run_required(runner, "llvm-profdata-merge", arguments)
        if not merged_path.is_file() or merged_path.stat().st_size <= 0:
            raise SingleBuildAnalysisError(
                "merged_profdata_missing", "llvm-profdata did not create merged.profdata", 4
            )
        self._fsync_file(merged_path)

    def _show_merged_profile(self, runner: object, merged_path: Path) -> None:
        """Records a readable summary of the newly merged profile."""

        self._run_required(
            runner,
            "llvm-profdata-show-merged",
            ["xcrun", "llvm-profdata", "show", "--binary-ids", str(merged_path)],
        )

    def _export_coverage(
        self, runner: object, coverage_binary: Path, merged_path: Path, export_path: Path
    ) -> None:
        """Exports complete source-level JSON from only the manifest-selected Mach-O."""

        self._run_required(
            runner,
            "llvm-cov-export",
            [
                "xcrun",
                "llvm-cov",
                "export",
                str(coverage_binary),
                "-instr-profile=" + str(merged_path),
                "-arch=arm64",
            ],
            stdout_filename=export_path.name,
        )
        if not export_path.is_file() or export_path.stat().st_size <= 0:
            raise SingleBuildAnalysisError(
                "llvm_cov_export_missing", "llvm-cov did not create coverage JSON", 4
            )
        self._fsync_file(export_path)

    def _run_required(
        self,
        runner: object,
        label: str,
        arguments: Sequence[str],
        stdout_filename: Optional[str] = None,
    ) -> object:
        """Rejects every nonzero exit and every nonempty stderr without a whitelist."""

        result = runner.run(label, arguments, stdout_filename=stdout_filename)
        if result.returncode != 0:
            raise SingleBuildAnalysisError(
                "llvm_command_failed", label + " exited with code " + str(result.returncode), 4
            )
        if result.stderr:
            raise SingleBuildAnalysisError(
                "llvm_stderr_not_empty", label + " emitted stderr", 4
            )
        return result

    def _load_export(self, export_path: Path) -> Dict[str, object]:
        """Validates the minimum llvm-cov JSON envelope needed by later stages."""

        try:
            payload = json.loads(export_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise SingleBuildAnalysisError(
                "llvm_cov_export_invalid", "llvm-cov export is not valid JSON", 4
            ) from error
        if (
            not isinstance(payload, dict)
            or payload.get("type") != "llvm.coverage.json.export"
            or not isinstance(payload.get("version"), str)
            or not isinstance(payload.get("data"), list)
            or len(payload["data"]) != 1
            or not isinstance(payload["data"][0], dict)
            or not isinstance(payload["data"][0].get("totals"), dict)
        ):
            raise SingleBuildAnalysisError(
                "llvm_cov_export_invalid", "llvm-cov export has an unsupported envelope", 4
            )
        return payload

    def _build_inputs_document(
        self,
        build: Mapping[str, object],
        manifest: Mapping[str, object],
        runs: Sequence[Mapping[str, object]],
        actual_uuids: Mapping[str, Sequence[str]],
    ) -> Dict[str, object]:
        """Builds the canonical audited input document without arbitrary user paths."""

        return {
            "schema_version": ANALYSIS_SCHEMA_VERSION,
            "cycle_id": build["cycle_id"],
            "build_id": build["id"],
            "build": {
                "git_sha": build["git_sha"],
                "target_sha_at_build": build["target_sha_at_build"],
                "merge_base_sha": build["merge_base_sha"],
                "coverage_binary": build["coverage_binary"],
                "manifest_sha256": build["manifest_sha256"],
                "manifest": manifest,
                "actual_macho_uuids": actual_uuids,
            },
            "runs": [
                {
                    "run_id": run["id"],
                    "payload_size": run["payload_size"],
                    "payload_sha256": run["payload_sha256"],
                    "metadata_sha256": run["metadata_sha256"],
                }
                for run in runs
            ],
        }

    def _input_set_sha256(self, inputs: Mapping[str, object]) -> str:
        """Hashes only normalized Build and sorted Run identities for stable idempotency."""

        build = inputs["build"]
        identity = {
            "schema_version": ANALYSIS_SCHEMA_VERSION,
            "cycle_id": inputs["cycle_id"],
            "build_id": inputs["build_id"],
            "manifest_sha256": build["manifest_sha256"],
            "runs": [
                {
                    "run_id": run["run_id"],
                    "payload_size": run["payload_size"],
                    "payload_sha256": run["payload_sha256"],
                }
                for run in inputs["runs"]
            ],
        }
        return hashlib.sha256(self._canonical_json_bytes(identity)).hexdigest()

    def _build_summary(
        self,
        build: Mapping[str, object],
        runs: Sequence[Mapping[str, object]],
        input_set_sha256: str,
        merged_path: Path,
        tool_versions: Mapping[str, object],
        export_payload: Mapping[str, object],
        started_at: str,
        completed_at: str,
    ) -> Dict[str, object]:
        """Creates the minimum source-level single-Build summary without MR semantics."""

        totals = export_payload["data"][0]["totals"]
        merged_size, merged_sha = self._file_identity(merged_path, "merged.profdata")
        coverage = {
            name: self._coverage_metric(totals, name)
            for name in ("functions", "lines", "regions")
        }
        return {
            "schema_version": ANALYSIS_SCHEMA_VERSION,
            "cycle_id": build["cycle_id"],
            "build_id": build["id"],
            "git_sha": build["git_sha"],
            "target_sha_at_build": build["target_sha_at_build"],
            "merge_base_sha": build["merge_base_sha"],
            "coverage_binary": build["coverage_binary"],
            "manifest_sha256": build["manifest_sha256"],
            "input_set_sha256": input_set_sha256,
            "run_ids": [run["id"] for run in runs],
            "profraw_count": len(runs),
            "merged_profdata": {"size": merged_size, "sha256": merged_sha},
            "tool_versions": tool_versions,
            "llvm_cov_export_format_version": export_payload["version"],
            "coverage": coverage,
            "started_at": started_at,
            "completed_at": completed_at,
            "status": "complete",
            "warnings": [],
        }

    def _coverage_metric(
        self, totals: Mapping[str, object], metric_name: str
    ) -> Dict[str, object]:
        """Normalizes one llvm-cov total without interpreting execution frequency."""

        metric = totals.get(metric_name)
        if not isinstance(metric, dict):
            raise SingleBuildAnalysisError(
                "llvm_cov_export_invalid", "llvm-cov totals are missing " + metric_name, 4
            )
        covered = metric.get("covered")
        total = metric.get("count")
        percent = metric.get("percent")
        if not isinstance(covered, int) or not isinstance(total, int) or not isinstance(
            percent, (int, float)
        ):
            raise SingleBuildAnalysisError(
                "llvm_cov_export_invalid", "llvm-cov total is invalid: " + metric_name, 4
            )
        return {"covered": covered, "total": total, "percent": percent}

    def _validate_existing_result(
        self, final_dir: Path, inputs: Mapping[str, object], input_set_sha256: str
    ) -> Mapping[str, object]:
        """Returns only a complete existing result whose recorded bytes still match."""

        try:
            stored_inputs = json.loads((final_dir / "inputs.json").read_text(encoding="utf-8"))
            completion = json.loads((final_dir / "completion.json").read_text(encoding="utf-8"))
            summary = json.loads((final_dir / "summary.json").read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise SingleBuildAnalysisError(
                "existing_result_invalid", "existing analysis result is incomplete or invalid", 5
            ) from error
        if stored_inputs != inputs:
            raise SingleBuildAnalysisError(
                "existing_result_conflict", "existing result inputs differ from the canonical input set", 5
            )
        if (
            completion.get("status") != "complete"
            or completion.get("input_set_sha256") != input_set_sha256
            or summary.get("status") != "complete"
            or summary.get("input_set_sha256") != input_set_sha256
        ):
            raise SingleBuildAnalysisError(
                "existing_result_invalid", "existing analysis completion marker is invalid", 5
            )
        outputs = completion.get("outputs")
        if not isinstance(outputs, dict):
            raise SingleBuildAnalysisError(
                "existing_result_invalid", "existing analysis output identity is missing", 5
            )
        actual_names = {
            str(path.relative_to(final_dir))
            for path in final_dir.rglob("*")
            if path.is_file() and path.name != "completion.json"
        }
        if actual_names != set(outputs):
            raise SingleBuildAnalysisError(
                "existing_result_invalid", "existing analysis output set has changed", 5
            )
        for relative_name, descriptor in outputs.items():
            if not isinstance(descriptor, dict):
                raise SingleBuildAnalysisError(
                    "existing_result_invalid", "existing analysis output identity is invalid", 5
                )
            size, sha256 = self._file_identity(final_dir / relative_name, "analysis output")
            if size != descriptor.get("size") or sha256 != descriptor.get("sha256"):
                raise SingleBuildAnalysisError(
                    "existing_result_invalid", "existing analysis output bytes have changed", 5
                )
        return summary

    def _publish_failure(
        self,
        staging_dir: Path,
        build_id: str,
        input_set_sha256: Optional[str],
        runner: object,
        error: SingleBuildAnalysisError,
    ) -> None:
        """Preserves failed command evidence outside the successful result namespace."""

        if not staging_dir.exists():
            return
        try:
            commands_path = staging_dir / "commands.json"
            if not commands_path.exists():
                self._write_json(commands_path, {"commands": runner.records})
            self._write_json(
                staging_dir / "failure.json",
                {
                    "schema_version": ANALYSIS_SCHEMA_VERSION,
                    "status": "failed",
                    "build_id": build_id,
                    "input_set_sha256": input_set_sha256,
                    "error": error.code,
                    "message": error.message,
                    "failed_at": self._utc_now(),
                },
            )
            self._fsync_directory(staging_dir)
            failure_parent = self.configuration.analysis_root / ".failed" / build_id
            failure_parent.mkdir(parents=True, exist_ok=True)
            failure_dir = failure_parent / (self._utc_compact() + "-" + uuid.uuid4().hex)
            os.rename(str(staging_dir), str(failure_dir))
            self._fsync_directory(failure_parent)
        except Exception:
            shutil.rmtree(str(staging_dir), ignore_errors=True)

    def _directory_file_identity(self, directory: Path) -> Dict[str, Dict[str, object]]:
        """Hashes every staged output before writing the completion marker."""

        result: Dict[str, Dict[str, object]] = {}
        for path in sorted(candidate for candidate in directory.rglob("*") if candidate.is_file()):
            relative_name = str(path.relative_to(directory))
            size, sha256 = self._file_identity(path, "analysis output")
            result[relative_name] = {"size": size, "sha256": sha256}
        return result

    def _artifact_paths(
        self, manifest: Mapping[str, object], cycle_id: str, build_id: str
    ) -> Dict[str, Path]:
        """Resolves only server-owned artifact names beneath the fixed data root."""

        artifact_root = self._build_directory(cycle_id, build_id) / "artifacts"
        return {
            field_name: artifact_root / ARTIFACT_STORAGE_NAMES[field_name]
            for field_name in manifest["artifacts"]
        }

    def _readonly_database(self) -> sqlite3.Connection:
        """Opens SQLite in URI read-only and query-only modes."""

        database = self.configuration.database_path.resolve()
        if not database.is_file():
            raise SingleBuildAnalysisError("database_not_found", "coverage SQLite database is missing")
        uri = "file:" + quote(str(database), safe="/") + "?mode=ro"
        connection = sqlite3.connect(uri, uri=True, timeout=10, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only = ON")
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    def _make_staging_directory(self, build_id: str) -> Path:
        """Creates an unpublished staging directory outside the final result namespace."""

        staging_root = self.configuration.analysis_root / ".staging"
        staging_root.mkdir(parents=True, exist_ok=True)
        return Path(tempfile.mkdtemp(prefix="." + build_id + "-", dir=str(staging_root)))

    def _build_directory(self, cycle_id: str, build_id: str) -> Path:
        """Returns one server-generated immutable Build path."""

        return self.configuration.data_dir / "cycles" / cycle_id / "builds" / build_id

    def _run_directory(self, cycle_id: str, run_id: str) -> Path:
        """Returns one server-generated immutable Run path."""

        return self.configuration.data_dir / "cycles" / cycle_id / "runs" / run_id

    def _file_identity(self, path: Path, label: str) -> Tuple[int, str]:
        """Returns exact size and SHA-256 for one required regular file."""

        if not path.is_file():
            raise SingleBuildAnalysisError("stored_file_missing", label + " is missing")
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
            raise SingleBuildAnalysisError("stored_file_unreadable", label + " cannot be read") from error
        return size, digest.hexdigest()

    def _write_json(self, destination: Path, payload: object) -> None:
        """Writes one deterministic JSON file and fsyncs it before publication."""

        encoded = json.dumps(
            payload, ensure_ascii=False, sort_keys=True, indent=2
        ).encode("utf-8") + b"\n"
        with destination.open("xb") as output:
            output.write(encoded)
            output.flush()
            os.fsync(output.fileno())

    def _fsync_file(self, path: Path) -> None:
        """Flushes a tool-generated output before directory publication."""

        with path.open("rb") as source:
            os.fsync(source.fileno())

    def _fsync_directory(self, directory: Path) -> None:
        """Flushes one directory entry on macOS."""

        descriptor = os.open(str(directory), os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def _normalize_uuid(self, value: str, field_name: str) -> str:
        """Normalizes one CLI UUID before it can influence a filesystem path."""

        try:
            return str(uuid.UUID(value))
        except (AttributeError, ValueError) as error:
            raise SingleBuildAnalysisError("invalid_uuid", field_name + " must be a UUID") from error

    def _canonical_json_bytes(self, payload: object) -> bytes:
        """Serializes stable semantic JSON for hashes and metadata checks."""

        return json.dumps(
            payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")

    def _artifact_validator(self, developer_dir: Path, command_runner: Callable) -> object:
        """Creates the existing safe artifact validator with strict stderr handling."""

        return XcodeArtifactValidator(
            developer_dir,
            command_runner=command_runner,
            reject_stderr=True,
        )

    def _utc_now(self) -> str:
        """Returns one timezone-explicit UTC timestamp."""

        return datetime.now(timezone.utc).isoformat()

    def _utc_compact(self) -> str:
        """Returns a filesystem-safe UTC failure timestamp."""

        return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
