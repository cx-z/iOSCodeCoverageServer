#!/usr/bin/env python3
"""One-click, read-only orchestration of all immutable analyses for one Cycle."""

import hashlib
import json
import os
import shutil
import sqlite3
import tempfile
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Callable, Dict, List, Mapping, Optional, Tuple
from urllib.parse import quote

from ioscoverage.analysis.cumulative.analyzer import CycleCoverageAnalysisError, CycleCoverageAnalyzer
from ioscoverage.analysis.single_build.analyzer import (
    AnalysisConfiguration,
    SingleBuildAnalysisError,
    SingleBuildAnalyzer,
)
from ioscoverage.analysis.diff.analyzer import (
    SingleBuildDiffAnalysisError,
    SingleBuildDiffAnalyzer,
)
from ioscoverage.storage.retained_evidence import RetainedBuildEvidenceStore


ORCHESTRATION_SCHEMA_VERSION = 1
ORCHESTRATOR_VERSION = "cycle-latest/1"


class CycleAnalysisOrchestrationError(Exception):
    """Carries one stable failure stage, error code, and CLI exit category."""

    def __init__(self, stage: str, code: str, message: str, exit_code: int = 3):
        """Creates one safe orchestration error without exposing private metadata."""

        super().__init__(message)
        self.stage = stage
        self.code = code
        self.message = message
        self.exit_code = exit_code


@dataclass(frozen=True)
class CycleAnalysisOrchestrationResult:
    """Returns one immutable orchestration result and replay status."""

    orchestration_input_sha256: str
    output_directory: Path
    summary: Mapping[str, object]
    idempotent_replay: bool


class CycleAnalysisOrchestrator:
    """Selects a consistent Cycle snapshot and coordinates existing analyzers."""

    def __init__(
        self,
        configuration: AnalysisConfiguration,
        single_build_analyzer: Optional[object] = None,
        diff_analyzer: Optional[object] = None,
        cycle_analyzer: Optional[object] = None,
        snapshot_observer: Optional[Callable[[Mapping[str, object]], None]] = None,
    ):
        """Injects analyzers for tests while retaining installation-owned paths."""

        self.configuration = configuration
        self.single_build_analyzer = single_build_analyzer or SingleBuildAnalyzer(configuration)
        self.diff_analyzer = diff_analyzer or SingleBuildDiffAnalyzer(configuration)
        self.cycle_analyzer = cycle_analyzer or CycleCoverageAnalyzer(configuration)
        self.snapshot_observer = snapshot_observer
        self.analysis_root = configuration.analysis_root.parent / "cycle-latest"
        self.retained_evidence_store = RetainedBuildEvidenceStore(
            configuration.analysis_root.parent
        )

    def analyze(self, cycle_id: str) -> CycleAnalysisOrchestrationResult:
        """Runs or reuses the complete immutable analysis selected from one DB snapshot."""

        cycle_id = self._normalize_uuid(cycle_id, "cycle_id", "snapshot")
        started_at = self._utc_now()
        staging_dir = self._make_staging_directory(cycle_id)
        identity_sha: Optional[str] = None
        stage = "snapshot"
        try:
            snapshot = self._load_snapshot(cycle_id)
            if self.snapshot_observer is not None:
                self.snapshot_observer(snapshot)

            stage = "build_selection"
            selection = self._select_inputs(snapshot)

            stage = "run_validation"
            self._validate_selected_files(selection)
            manifest = self._build_manifest(snapshot, selection)
            identity_sha = self._json_sha256(manifest)
            manifest["orchestration_input_sha256"] = identity_sha
            final_dir = self.analysis_root / cycle_id / identity_sha
            if final_dir.exists():
                summary = self._validate_existing_result(final_dir, manifest, identity_sha)
                shutil.rmtree(str(staging_dir), ignore_errors=True)
                return CycleAnalysisOrchestrationResult(identity_sha, final_dir, summary, True)

            stage = "single_build"
            build_results = []
            build_inputs: Dict[str, str] = {}
            for item in selection["selected_builds"]:
                build_id = str(item["build"]["id"])
                run_ids = [str(run["id"]) for run in item["runs"]]
                try:
                    result = self.single_build_analyzer.analyze(cycle_id, build_id, run_ids)
                except SingleBuildAnalysisError as error:
                    failure_stage = (
                        "run_validation"
                        if error.code.startswith("run_")
                        or error.code.startswith("profraw_")
                        or error.code.startswith("payload_")
                        or error.code.startswith("metadata_")
                        else "single_build"
                    )
                    raise CycleAnalysisOrchestrationError(
                        failure_stage, error.code, error.message, error.exit_code
                    ) from error
                build_inputs[build_id] = result.input_set_sha256
                build_results.append(self._single_build_reference(build_id, result))

            current_build_id = str(selection["current_build_id"])
            current_input = build_inputs[current_build_id]
            stage = "current_diff"
            try:
                diff_result = self.diff_analyzer.analyze(cycle_id, current_build_id, current_input)
            except SingleBuildDiffAnalysisError as error:
                raise CycleAnalysisOrchestrationError(
                    "current_diff", error.code, error.message, error.exit_code
                ) from error

            stage = "cumulative"
            try:
                cycle_result = self.cycle_analyzer.analyze(
                    cycle_id, current_build_id, build_inputs
                )
            except CycleCoverageAnalysisError as error:
                raise CycleAnalysisOrchestrationError(
                    "cumulative", error.code, error.message, error.exit_code
                ) from error

            results = {
                "schema_version": ORCHESTRATION_SCHEMA_VERSION,
                "single_build_results": build_results,
                "current_diff_result": self._result_reference(
                    diff_result.output_directory,
                    "completion.json",
                    "summary.json",
                    {"diff_set_sha256": diff_result.diff_set_sha256},
                ),
                "cycle_coverage_result": self._result_reference(
                    cycle_result.output_directory,
                    "complete.json",
                    "summary.json",
                    {"cycle_input_set_sha256": cycle_result.cycle_input_set_sha256},
                ),
            }
            completed_at = self._utc_now()
            summary = self._build_summary(
                snapshot,
                selection,
                identity_sha,
                build_inputs,
                diff_result,
                cycle_result,
                started_at,
                completed_at,
            )

            stage = "publication"
            self._write_json(staging_dir / "manifest.json", manifest)
            self._write_json(
                staging_dir / "selected-builds.json", selection["builds_document"]
            )
            self._write_json(
                staging_dir / "selected-runs.json", selection["runs_document"]
            )
            self._write_json(staging_dir / "results.json", results)
            self._write_json(staging_dir / "summary.json", summary)
            completion = {
                "schema_version": ORCHESTRATION_SCHEMA_VERSION,
                "status": "complete",
                "orchestration_input_sha256": identity_sha,
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
                existing = self._validate_existing_result(final_dir, manifest, identity_sha)
                shutil.rmtree(str(staging_dir), ignore_errors=True)
                return CycleAnalysisOrchestrationResult(identity_sha, final_dir, existing, True)
            self._fsync_directory(final_dir.parent)
            return CycleAnalysisOrchestrationResult(identity_sha, final_dir, summary, False)
        except CycleAnalysisOrchestrationError as error:
            self._publish_failure(staging_dir, cycle_id, identity_sha, error)
            raise
        except Exception as error:
            wrapped = CycleAnalysisOrchestrationError(
                stage,
                "orchestration_internal_error",
                "Cycle analysis orchestration failed unexpectedly",
                5,
            )
            self._publish_failure(staging_dir, cycle_id, identity_sha, wrapped)
            raise wrapped from error

    def _load_snapshot(self, cycle_id: str) -> Dict[str, object]:
        """Reads Cycle, Builds, and Runs in one read-only SQLite transaction."""

        connection = self._readonly_database()
        try:
            connection.execute("BEGIN")
            cycle_row = connection.execute(
                "SELECT * FROM coverage_cycles WHERE id = ?", (cycle_id,)
            ).fetchone()
            if cycle_row is None:
                raise CycleAnalysisOrchestrationError(
                    "snapshot", "cycle_not_found", "cycle_id is not registered"
                )
            build_rows = connection.execute(
                "SELECT * FROM builds WHERE cycle_id = ? ORDER BY created_at, id", (cycle_id,)
            ).fetchall()
            build_ids = [str(row["id"]) for row in build_rows]
            parameters: List[object] = [cycle_id]
            run_sql = "SELECT * FROM runs WHERE cycle_id = ?"
            if build_ids:
                run_sql += " OR build_id IN ({})".format(",".join("?" for _ in build_ids))
                parameters.extend(build_ids)
            run_sql += " ORDER BY build_id, id"
            run_rows = connection.execute(run_sql, parameters).fetchall()
            snapshot = {
                "cycle": dict(cycle_row),
                "builds": [dict(row) for row in build_rows],
                "runs": [dict(row) for row in run_rows],
            }
            connection.execute("COMMIT")
            return snapshot
        except CycleAnalysisOrchestrationError:
            try:
                connection.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise
        except sqlite3.Error as error:
            try:
                connection.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise CycleAnalysisOrchestrationError(
                "snapshot", "database_read_failed", "coverage SQLite snapshot could not be read", 5
            ) from error
        finally:
            connection.close()

    def _select_inputs(self, snapshot: Mapping[str, object]) -> Dict[str, object]:
        """Selects the current Build and all eligible historical Builds deterministically."""

        cycle = snapshot["cycle"]
        builds = list(snapshot["builds"])
        runs = list(snapshot["runs"])
        build_by_id = {str(build["id"]): build for build in builds}
        for run in runs:
            build = build_by_id.get(str(run["build_id"]))
            if build is None or run["cycle_id"] != cycle["id"]:
                raise CycleAnalysisOrchestrationError(
                    "build_selection",
                    "run_ownership_mismatch",
                    "a Run does not belong to its declared Cycle and Build",
                )

        candidates = [
            build
            for build in builds
            if build["artifact_state"] == "ready" and build["git_sha"] == cycle["head_sha"]
        ]
        if not candidates:
            raise CycleAnalysisOrchestrationError(
                "build_selection",
                "current_build_not_found",
                "no ready Build matches Cycle.head_sha",
            )
        current = max(candidates, key=lambda value: (str(value["created_at"]), str(value["id"])))
        current_created_at = str(current["created_at"])
        runs_by_build: Dict[str, List[Mapping[str, object]]] = {}
        for run in runs:
            runs_by_build.setdefault(str(run["build_id"]), []).append(run)

        selected = []
        build_records = []
        run_records = []
        for build in sorted(builds, key=lambda value: (str(value["created_at"]), str(value["id"]))):
            build_id = str(build["id"])
            build_runs = sorted(runs_by_build.get(build_id, []), key=lambda value: str(value["id"]))
            valid_runs = [run for run in build_runs if self._run_is_analyzable(run)]
            if build["artifact_state"] != "ready":
                decision, reason = "skipped", "artifact_not_ready"
            elif str(build["created_at"]) > current_created_at:
                decision, reason = "skipped", "after_current_build"
            elif not valid_runs:
                decision, reason = "skipped", "no_analyzable_runs"
            else:
                decision, reason = "selected", None
                selected.append({"build": build, "runs": valid_runs})
            build_records.append(
                {
                    "build_id": build_id,
                    "git_sha": build["git_sha"],
                    "created_at": build["created_at"],
                    "artifact_state": build["artifact_state"],
                    "manifest_sha256": build["manifest_sha256"],
                    "is_current": build_id == current["id"],
                    "decision": decision,
                    "skip_reason": reason,
                    "analyzable_run_count": len(valid_runs),
                }
            )
            valid_ids = {str(run["id"]) for run in valid_runs}
            for run in build_runs:
                selected_run = str(run["id"]) in valid_ids and decision == "selected"
                run_records.append(
                    {
                        "run_id": run["id"],
                        "build_id": build_id,
                        "selected": selected_run,
                        "skip_reason": None if selected_run else self._run_skip_reason(run, reason),
                        "state": run["state"],
                        "analyzable": bool(run["analyzable"]),
                        "payload_size": run["payload_size"],
                        "payload_sha256": run["payload_sha256"],
                        "metadata_sha256": run["metadata_sha256"],
                    }
                )

        selected_ids = {str(item["build"]["id"]) for item in selected}
        if str(current["id"]) not in selected_ids:
            raise CycleAnalysisOrchestrationError(
                "build_selection",
                "current_build_has_no_analyzable_runs",
                "the selected current Build has no analyzable Run",
            )
        return {
            "current_build_id": current["id"],
            "selected_builds": selected,
            "builds_document": {
                "schema_version": ORCHESTRATION_SCHEMA_VERSION,
                "current_build_id": current["id"],
                "selection_order": "created_at_then_build_id_ascending",
                "builds": build_records,
            },
            "runs_document": {
                "schema_version": ORCHESTRATION_SCHEMA_VERSION,
                "selection_order": "run_id_ascending_within_build",
                "runs": sorted(run_records, key=lambda value: (str(value["build_id"]), str(value["run_id"]))),
            },
        }

    def _validate_selected_files(self, selection: Mapping[str, object]) -> None:
        """Freezes selected manifest, metadata, and profraw identities before analysis."""

        for item in selection["selected_builds"]:
            build = item["build"]
            cycle_id = str(build["cycle_id"])
            build_id = str(build["id"])
            manifest_path = (
                self.configuration.data_dir
                / "cycles"
                / cycle_id
                / "builds"
                / build_id
                / "manifest.json"
            )
            manifest = self._load_json_file(manifest_path, "build_manifest_invalid")
            try:
                database_manifest = json.loads(str(build["manifest_json"]))
            except json.JSONDecodeError as error:
                raise CycleAnalysisOrchestrationError(
                    "run_validation", "build_manifest_invalid", "Build manifest JSON is invalid"
                ) from error
            if manifest != database_manifest or self._json_sha256(manifest) != build["manifest_sha256"]:
                raise CycleAnalysisOrchestrationError(
                    "run_validation",
                    "build_manifest_mismatch",
                    "Build manifest does not match SQLite or its SHA-256",
                )
            retained_baseline = self.retained_evidence_store.find_compatible(
                cycle_id, build_id, build["manifest_sha256"], item["runs"]
            )
            for run in item["runs"]:
                run_dir = self.configuration.data_dir / "cycles" / cycle_id / "runs" / str(run["id"])
                metadata = self._load_json_file(run_dir / "metadata.json", "run_metadata_invalid")
                try:
                    database_metadata = json.loads(str(run["metadata_json"]))
                except json.JSONDecodeError as error:
                    raise CycleAnalysisOrchestrationError(
                        "run_validation", "run_metadata_invalid", "Run metadata JSON is invalid"
                    ) from error
                if metadata != database_metadata or self._json_sha256(metadata) != run["metadata_sha256"]:
                    raise CycleAnalysisOrchestrationError(
                        "run_validation",
                        "run_metadata_mismatch",
                        "Run metadata does not match SQLite or its SHA-256",
                    )
                payload_path = run_dir / "payload.profraw"
                if (
                    not payload_path.is_file()
                    and retained_baseline is not None
                    and self.retained_evidence_store.run_matches(retained_baseline, run)
                ):
                    continue
                size, sha = self._file_identity(payload_path, "Run profraw")
                if size != run["payload_size"] or sha != run["payload_sha256"]:
                    raise CycleAnalysisOrchestrationError(
                        "run_validation",
                        "profraw_identity_mismatch",
                        "Run profraw does not match SQLite size or SHA-256",
                    )

    def _build_manifest(
        self, snapshot: Mapping[str, object], selection: Mapping[str, object]
    ) -> Dict[str, object]:
        """Builds the canonical orchestration identity from the frozen selection."""

        cycle = snapshot["cycle"]
        selected_builds = []
        for item in selection["selected_builds"]:
            build = item["build"]
            selected_builds.append(
                {
                    "build_id": build["id"],
                    "cycle_id": build["cycle_id"],
                    "git_sha": build["git_sha"],
                    "target_sha_at_build": build["target_sha_at_build"],
                    "merge_base_sha": build["merge_base_sha"],
                    "coverage_binary": build["coverage_binary"],
                    "created_at": build["created_at"],
                    "manifest_sha256": build["manifest_sha256"],
                    "artifacts": self._artifact_database_identity(build),
                    "runs": [self._run_identity(run) for run in item["runs"]],
                }
            )
        skipped = [
            {
                "build_id": record["build_id"],
                "git_sha": record["git_sha"],
                "created_at": record["created_at"],
                "manifest_sha256": record["manifest_sha256"],
                "skip_reason": record["skip_reason"],
            }
            for record in selection["builds_document"]["builds"]
            if record["decision"] == "skipped"
        ]
        return {
            "schema_version": ORCHESTRATION_SCHEMA_VERSION,
            "orchestrator_version": ORCHESTRATOR_VERSION,
            "cycle": {
                "cycle_id": cycle["id"],
                "schema_version": cycle["schema_version"],
                "project": cycle["project"],
                "target_branch": cycle["target_branch"],
                "base_sha": cycle["base_sha"],
                "head_sha": cycle["head_sha"],
                "status": cycle["status"],
                "created_at": cycle["created_at"],
                "updated_at": cycle["updated_at"],
            },
            "current_build_id": selection["current_build_id"],
            "selected_builds": selected_builds,
            "skipped_builds": skipped,
            "sorting": {
                "current_build": "max(created_at, build_id) among ready Builds matching Cycle.head_sha",
                "builds": "created_at then build_id ascending",
                "runs": "run_id ascending",
            },
        }

    def _build_summary(
        self,
        snapshot: Mapping[str, object],
        selection: Mapping[str, object],
        identity_sha: str,
        build_inputs: Mapping[str, str],
        diff_result: object,
        cycle_result: object,
        started_at: str,
        completed_at: str,
    ) -> Dict[str, object]:
        """Adds orchestration facts around the existing cumulative summary."""

        cumulative = dict(cycle_result.summary)
        return {
            "schema_version": ORCHESTRATION_SCHEMA_VERSION,
            "status": "complete",
            "cycle_id": snapshot["cycle"]["id"],
            "orchestration_input_sha256": identity_sha,
            "current_build_id": selection["current_build_id"],
            "selected_build_count": len(selection["selected_builds"]),
            "selected_run_count": sum(len(item["runs"]) for item in selection["selected_builds"]),
            "skipped_build_count": sum(
                record["decision"] == "skipped"
                for record in selection["builds_document"]["builds"]
            ),
            "build_inputs": [
                {"build_id": build_id, "input_set_sha256": input_sha}
                for build_id, input_sha in sorted(build_inputs.items())
            ],
            "current_diff_set_sha256": diff_result.diff_set_sha256,
            "cycle_input_set_sha256": cycle_result.cycle_input_set_sha256,
            "cumulative_summary": cumulative,
            "started_at": started_at,
            "completed_at": completed_at,
        }

    def _single_build_reference(self, build_id: str, result: object) -> Dict[str, object]:
        """Records hashes of one immutable single-Build result without copying it."""

        return self._result_reference(
            result.output_directory,
            "completion.json",
            "summary.json",
            {"build_id": build_id, "input_set_sha256": result.input_set_sha256},
        )

    def _result_reference(
        self,
        directory: Path,
        completion_name: str,
        summary_name: str,
        identity: Mapping[str, object],
    ) -> Dict[str, object]:
        """Creates an audited pointer to one completion-protected downstream result."""

        completion_size, completion_sha = self._file_identity(
            Path(directory) / completion_name, "downstream completion", "publication"
        )
        summary_size, summary_sha = self._file_identity(
            Path(directory) / summary_name, "downstream summary", "publication"
        )
        return {
            **identity,
            "output_directory": str(Path(directory).resolve()),
            "completion": {"size": completion_size, "sha256": completion_sha},
            "summary": {"size": summary_size, "sha256": summary_sha},
        }

    def _validate_existing_result(
        self, directory: Path, manifest: Mapping[str, object], identity_sha: str
    ) -> Mapping[str, object]:
        """Reuses only an exact result whose own and referenced bytes remain intact."""

        stored_manifest = self._load_json_file(
            directory / "manifest.json", "existing_result_invalid", "publication"
        )
        summary = self._load_json_file(
            directory / "summary.json", "existing_result_invalid", "publication"
        )
        completion = self._load_json_file(
            directory / "complete.json", "existing_result_invalid", "publication"
        )
        if stored_manifest != manifest:
            raise CycleAnalysisOrchestrationError(
                "publication", "existing_result_conflict", "existing orchestration inputs differ", 5
            )
        if (
            completion.get("status") != "complete"
            or completion.get("orchestration_input_sha256") != identity_sha
            or summary.get("status") != "complete"
            or summary.get("orchestration_input_sha256") != identity_sha
        ):
            raise CycleAnalysisOrchestrationError(
                "publication", "existing_result_invalid", "existing completion is invalid", 5
            )
        outputs = completion.get("outputs")
        if not isinstance(outputs, dict):
            raise CycleAnalysisOrchestrationError(
                "publication", "existing_result_invalid", "existing output identity is missing", 5
            )
        actual = {
            path.relative_to(directory).as_posix()
            for path in directory.rglob("*")
            if path.is_file() and path.name != "complete.json"
        }
        if actual != set(outputs):
            raise CycleAnalysisOrchestrationError(
                "publication", "existing_result_invalid", "existing output set changed", 5
            )
        for relative, descriptor in outputs.items():
            safe = self._safe_relative_name(relative)
            size, sha = self._file_identity(
                directory / safe, "orchestration output", "publication"
            )
            if not isinstance(descriptor, dict) or size != descriptor.get("size") or sha != descriptor.get("sha256"):
                raise CycleAnalysisOrchestrationError(
                    "publication", "existing_result_invalid", "existing output bytes changed", 5
                )
        results = self._load_json_file(
            directory / "results.json", "existing_result_invalid", "publication"
        )
        single_references = results.get("single_build_results")
        if not isinstance(single_references, list):
            raise CycleAnalysisOrchestrationError(
                "publication", "existing_result_invalid", "downstream result list is invalid", 5
            )
        references = list(single_references)
        references.extend(
            [results.get("current_diff_result"), results.get("cycle_coverage_result")]
        )
        for reference in references:
            self._validate_result_reference(reference)
        return summary

    def _validate_result_reference(self, reference: object) -> None:
        """Checks that a replay still points to the exact downstream summary/completion."""

        if not isinstance(reference, dict) or not isinstance(reference.get("output_directory"), str):
            raise CycleAnalysisOrchestrationError(
                "publication", "existing_result_invalid", "downstream result reference is invalid", 5
            )
        directory = Path(reference["output_directory"]).resolve()
        if "input_set_sha256" in reference:
            expected_root = self.configuration.analysis_root.resolve()
        elif "diff_set_sha256" in reference:
            expected_root = (self.configuration.analysis_root.parent / "single-build-diff").resolve()
        elif "cycle_input_set_sha256" in reference:
            expected_root = (self.configuration.analysis_root.parent / "cycle-coverage").resolve()
        else:
            raise CycleAnalysisOrchestrationError(
                "publication", "existing_result_invalid", "downstream result kind is invalid", 5
            )
        try:
            directory.relative_to(expected_root)
        except ValueError as error:
            raise CycleAnalysisOrchestrationError(
                "publication", "existing_result_invalid", "downstream result path escaped its fixed root", 5
            ) from error
        for key, filenames in (("completion", ("completion.json", "complete.json")), ("summary", ("summary.json",))):
            descriptor = reference.get(key)
            if not isinstance(descriptor, dict):
                raise CycleAnalysisOrchestrationError(
                    "publication", "existing_result_invalid", "downstream identity is invalid", 5
                )
            path = next((directory / name for name in filenames if (directory / name).is_file()), None)
            if path is None:
                raise CycleAnalysisOrchestrationError(
                    "publication", "existing_result_invalid", "downstream result is missing", 5
                )
            size, sha = self._file_identity(path, "downstream result", "publication")
            if size != descriptor.get("size") or sha != descriptor.get("sha256"):
                raise CycleAnalysisOrchestrationError(
                    "publication", "existing_result_invalid", "downstream result bytes changed", 5
                )

    def _artifact_database_identity(self, build: Mapping[str, object]) -> Dict[str, object]:
        """Returns immutable artifact descriptors already enforced by SQLite triggers."""

        result = {
            "main_executable": {
                "size": build["main_executable_size"],
                "sha256": build["main_executable_sha256"],
            },
            "dsym": {"size": build["dsym_size"], "sha256": build["dsym_sha256"]},
        }
        if build.get("debug_dylib_sha256") is not None:
            result["debug_dylib"] = {
                "size": build["debug_dylib_size"],
                "sha256": build["debug_dylib_sha256"],
            }
        return result

    def _run_identity(self, run: Mapping[str, object]) -> Dict[str, object]:
        """Returns the stable identity of one selected immutable Run."""

        return {
            "run_id": run["id"],
            "cycle_id": run["cycle_id"],
            "build_id": run["build_id"],
            "metadata_sha256": run["metadata_sha256"],
            "payload_size": run["payload_size"],
            "payload_sha256": run["payload_sha256"],
        }

    def _run_is_analyzable(self, run: Mapping[str, object]) -> bool:
        """Applies the stable Run eligibility predicate."""

        return (
            run["state"] == "uploaded"
            and bool(run["analyzable"])
            and (run["error_message"] is None or run["error_message"] == "")
        )

    def _run_skip_reason(self, run: Mapping[str, object], build_reason: Optional[str]) -> str:
        """Explains why a Run was excluded without silently dropping it."""

        if build_reason is not None:
            return build_reason
        if run["state"] != "uploaded":
            return "state_not_uploaded"
        if not bool(run["analyzable"]):
            return "not_analyzable"
        if run["error_message"] not in (None, ""):
            return "error_message_present"
        return "not_selected"

    def _readonly_database(self) -> sqlite3.Connection:
        """Opens the configured service database read-only and query-only."""

        database = self.configuration.database_path.resolve()
        if not database.is_file():
            raise CycleAnalysisOrchestrationError(
                "snapshot", "database_not_found", "coverage SQLite database is missing"
            )
        uri = "file:" + quote(str(database), safe="/") + "?mode=ro"
        connection = sqlite3.connect(uri, uri=True, timeout=10, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only = ON")
        return connection

    def _publish_failure(
        self,
        staging_dir: Path,
        cycle_id: str,
        identity_sha: Optional[str],
        error: CycleAnalysisOrchestrationError,
    ) -> None:
        """Publishes bounded failure evidence outside every success directory."""

        if not staging_dir.exists():
            return
        try:
            self._write_json(
                staging_dir / "failure.json",
                {
                    "schema_version": ORCHESTRATION_SCHEMA_VERSION,
                    "status": "failed",
                    "cycle_id": cycle_id,
                    "orchestration_input_sha256": identity_sha,
                    "failure_stage": error.stage,
                    "error": error.code,
                    "message": error.message,
                    "failed_at": self._utc_now(),
                },
            )
            self._fsync_directory(staging_dir)
            parent = self.analysis_root / ".failed" / cycle_id
            parent.mkdir(parents=True, exist_ok=True)
            destination = parent / (self._utc_compact() + "-" + uuid.uuid4().hex)
            os.rename(str(staging_dir), str(destination))
            self._fsync_directory(parent)
        except Exception:
            shutil.rmtree(str(staging_dir), ignore_errors=True)

    def _make_staging_directory(self, cycle_id: str) -> Path:
        """Creates one unpublished same-filesystem staging directory."""

        root = self.analysis_root / ".staging"
        root.mkdir(parents=True, exist_ok=True)
        return Path(tempfile.mkdtemp(prefix="." + cycle_id + "-", dir=str(root)))

    def _directory_file_identity(self, directory: Path) -> Dict[str, Dict[str, object]]:
        """Hashes every staged evidence file before complete.json is created."""

        result = {}
        for path in sorted(item for item in directory.rglob("*") if item.is_file()):
            size, sha = self._file_identity(path, "orchestration output", "publication")
            result[path.relative_to(directory).as_posix()] = {"size": size, "sha256": sha}
        return result

    def _file_identity(
        self, path: Path, label: str, stage: str = "run_validation"
    ) -> Tuple[int, str]:
        """Hashes one required regular non-symlink file."""

        if not path.is_file() or path.is_symlink():
            raise CycleAnalysisOrchestrationError(
                stage, "stored_file_missing", label + " is missing"
            )
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
            raise CycleAnalysisOrchestrationError(
                stage, "stored_file_unreadable", label + " cannot be read"
            ) from error
        return size, digest.hexdigest()

    def _load_json_file(
        self, path: Path, code: str, stage: str = "run_validation"
    ) -> Dict[str, object]:
        """Loads one required JSON object without following a symlink."""

        if path.is_symlink():
            raise CycleAnalysisOrchestrationError(stage, code, path.name + " is unsafe")
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
            raise CycleAnalysisOrchestrationError(
                stage, code, path.name + " is missing or invalid"
            ) from error
        if not isinstance(value, dict):
            raise CycleAnalysisOrchestrationError(
                stage, code, path.name + " must contain an object"
            )
        return value

    def _safe_relative_name(self, value: object) -> str:
        """Rejects completion paths capable of escaping a result directory."""

        if not isinstance(value, str) or not value:
            raise CycleAnalysisOrchestrationError(
                "publication", "result_output_path_invalid", "output path is invalid", 5
            )
        path = PurePosixPath(value)
        if path.is_absolute() or path.as_posix() != value or ".." in path.parts:
            raise CycleAnalysisOrchestrationError(
                "publication", "result_output_path_invalid", "output path is unsafe", 5
            )
        return value

    def _normalize_uuid(self, value: str, field: str, stage: str) -> str:
        """Accepts only canonical UUID identities."""

        try:
            normalized = str(uuid.UUID(value))
        except (ValueError, AttributeError, TypeError) as error:
            raise CycleAnalysisOrchestrationError(stage, "invalid_" + field, field + " must be a UUID") from error
        if normalized != value:
            raise CycleAnalysisOrchestrationError(stage, "invalid_" + field, field + " must be canonical")
        return normalized

    def _json_sha256(self, payload: Mapping[str, object]) -> str:
        """Hashes canonical JSON for all stable orchestration identities."""

        return hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        ).hexdigest()

    def _write_json(self, path: Path, payload: Mapping[str, object]) -> None:
        """Writes and fsyncs one deterministic JSON evidence file."""

        with path.open("w", encoding="utf-8") as destination:
            json.dump(payload, destination, sort_keys=True, indent=2, ensure_ascii=False)
            destination.write("\n")
            destination.flush()
            os.fsync(destination.fileno())

    def _fsync_directory(self, path: Path) -> None:
        """Persists directory entries before returning a published result."""

        descriptor = os.open(str(path), os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def _utc_now(self) -> str:
        """Returns one stable UTC timestamp representation."""

        return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

    def _utc_compact(self) -> str:
        """Returns one sortable UTC failure directory prefix."""

        return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
