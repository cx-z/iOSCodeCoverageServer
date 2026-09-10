"""Dependency-driven cleanup of runtime data after durable analysis publication."""

import hashlib
import json
import os
import sqlite3
import uuid
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Dict, Iterable, List, Mapping, Optional, Sequence, Set, Tuple

from ioscoverage.runtime import RuntimePaths
from ioscoverage.storage.coverage_store import ARTIFACT_STORAGE_NAMES
from ioscoverage.storage.reports import CoverageReportError, CoverageReportStore
from ioscoverage.storage.retained_evidence import RetainedBuildEvidenceStore, RetainedEvidenceError
from ioscoverage.lifecycle.runtime_lock import RuntimeLockError, RuntimeProcessLock
from ioscoverage.lifecycle.secure_delete import (
    SecureDeletionError,
    SecureRuntimeDeletion,
    TreeEntry,
)


class RuntimeCleanupError(RuntimeError):
    """Carries a stable cleanup failure safe for terminal output."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class CleanupCandidate:
    """One exact file or completed result directory eligible for deletion."""

    path: Path
    category: str
    size: int
    sha256: Optional[str] = None
    tree_entries: Tuple[TreeEntry, ...] = ()


@dataclass(frozen=True)
class CompletedGeneration:
    """One completion-proven result with identities derived from its fixed layout."""

    path: Path
    kind: str
    cycle_id: str
    build_id: Optional[str]
    identity_sha256: str


class _FixedAnalysisState:
    """Supplies one already-read state row to the report verifier."""

    def __init__(self, state: Mapping[str, object]):
        self.state = state

    def get_state(self, cycle_id: str) -> Mapping[str, object]:
        if self.state.get("cycle_id") != cycle_id:
            raise ValueError("Cycle state mismatch")
        return self.state


class RuntimeDataCleaner:
    """Computes retention first and deletes only completion-proven redundant data."""

    def __init__(self, paths: RuntimePaths):
        self.paths = paths
        self.root = paths.root.resolve()
        self.evidence_store = RetainedBuildEvidenceStore(paths.analysis_dir)

    def run(self, apply: bool = False) -> Dict[str, object]:
        """Returns a machine-readable dry-run or applies the exact safe plan."""

        lock: Optional[RuntimeProcessLock] = None
        if apply:
            lock = RuntimeProcessLock(self.paths.server_lock)
            try:
                lock.acquire()
            except RuntimeLockError as error:
                raise RuntimeCleanupError(
                    "service_running", "coverage server or offline analysis is still running"
                ) from error
        try:
            return self._run_locked(apply)
        except RuntimeCleanupError:
            raise
        except RetainedEvidenceError as error:
            raise RuntimeCleanupError(
                "retained_evidence_failed", "retained Build evidence could not be published"
            ) from error
        except SecureDeletionError as error:
            raise RuntimeCleanupError(error.code, error.message) from error
        except sqlite3.Error as error:
            raise RuntimeCleanupError(
                "database_read_failed", "coverage lifecycle state could not be verified"
            ) from error
        except OSError as error:
            raise RuntimeCleanupError(
                "cleanup_io_failed", "runtime cleanup could not complete filesystem changes"
            ) from error
        finally:
            if lock is not None:
                lock.release()

    def _run_locked(self, apply: bool) -> Dict[str, object]:
        """Plans and applies while the caller owns the runtime lock when destructive."""

        if not self.paths.database.is_file():
            raise RuntimeCleanupError("database_not_found", "coverage SQLite database is missing")
        bytes_before = self._managed_bytes()
        unsafe_links = self._unsafe_symlinks()
        snapshot = self._snapshot()
        active = [state for state in snapshot["states"] if state["status"] in ("queued", "running")]
        if apply and unsafe_links:
            raise RuntimeCleanupError("unsafe_runtime_symlink", "runtime data contains a symbolic link")
        if apply and active:
            raise RuntimeCleanupError("analysis_active", "queued or running analysis prevents cleanup")

        retained: Set[Path] = set()
        candidates: Dict[Path, CleanupCandidate] = {}
        evidence_specs: List[Tuple[str, str, Path, Sequence[str]]] = []
        eligible_cycles: Set[str] = set()
        eligible_builds: Set[str] = set()
        skipped: List[Dict[str, object]] = []
        generations = list(self._completed_generations())
        generation_count = len(generations)
        builds = {str(item["id"]): item for item in snapshot["builds"]}
        runs = {str(item["id"]): item for item in snapshot["runs"]}

        for state in snapshot["states"]:
            cycle_id = str(state["cycle_id"])
            orchestration_sha = state.get("latest_success_orchestration_sha256")
            if not isinstance(orchestration_sha, str):
                skipped.append({"cycle_id": cycle_id, "reason": "no_latest_success"})
                continue
            cycle_retained: Set[Path] = set()
            cycle_candidates: Dict[Path, CleanupCandidate] = {}
            cycle_evidence: List[Tuple[str, str, Path, Sequence[str]]] = []
            cycle_builds: Set[str] = set()
            try:
                CoverageReportStore(self.paths.analysis_dir, _FixedAnalysisState(state)).get_report(cycle_id)
                orchestration_dir = self.paths.analysis_dir / "cycle-latest" / cycle_id / orchestration_sha
                current_build_id = self._canonical_uuid(
                    state.get("latest_success_current_build_id"), "latest success current Build"
                )
                cycle_input_sha = self._canonical_sha256(
                    state.get("latest_success_cycle_input_sha256"), "latest success Cycle input"
                )
                results = self._validate_orchestration_generation(
                    orchestration_dir, cycle_id, orchestration_sha,
                    current_build_id, cycle_input_sha, state,
                )
                self._protect_reference(cycle_retained, orchestration_dir)
                single_results = results.get("single_build_results")
                if not isinstance(single_results, list):
                    raise RuntimeCleanupError("latest_success_invalid", "single-Build result list is invalid")
                input_by_build: Dict[str, str] = {}
                for reference in single_results:
                    if not isinstance(reference, dict):
                        raise RuntimeCleanupError("latest_success_invalid", "single-Build reference is invalid")
                    build_id = self._canonical_uuid(reference.get("build_id"), "single-Build reference")
                    input_sha = self._canonical_sha256(
                        reference.get("input_set_sha256"), "single-Build input"
                    )
                    if build_id in input_by_build:
                        raise RuntimeCleanupError("latest_success_invalid", "single-Build reference is duplicated")
                    input_by_build[build_id] = input_sha
                    cycle_builds.add(build_id)
                    result_dir = self._exact_reference_directory(
                        reference,
                        self.paths.analysis_dir / "single-build" / build_id / input_sha,
                    )
                    self._validate_reference_files(reference, result_dir, "completion.json")
                    validated = self.evidence_store.validate_result(result_dir, cycle_id, build_id)
                    inputs = validated["inputs"]
                    build = builds.get(build_id)
                    self._validate_build_snapshot(build, inputs, cycle_id, build_id)
                    input_runs = inputs.get("runs")
                    if not isinstance(input_runs, list):
                        raise RuntimeCleanupError(
                            "latest_success_invalid", "single-Build Run list is invalid"
                        )
                    selected_runs = [
                        self._validate_run_snapshot(item, runs, cycle_id, build_id)
                        for item in input_runs
                    ]
                    coverage_binary = str(build["coverage_binary"])
                    artifacts = inputs["build"]["manifest"]["artifacts"]
                    removed_roles = sorted(role for role in artifacts if role != coverage_binary)
                    baseline = self.evidence_store.find_compatible(
                        cycle_id, build_id, build["manifest_sha256"], selected_runs
                    )
                    cycle_evidence.append((cycle_id, build_id, result_dir, removed_roles))
                    cycle_retained.add(
                        self.evidence_store.evidence_directory(
                            cycle_id, build_id, str(inputs["input_set_sha256"])
                        ).resolve()
                    )
                    self._protect_reference(cycle_retained, result_dir)
                    artifact_root = self.paths.data_dir / "cycles" / cycle_id / "builds" / build_id / "artifacts"
                    for role, descriptor in artifacts.items():
                        artifact_path = artifact_root / ARTIFACT_STORAGE_NAMES[role]
                        if role == coverage_binary:
                            self._assert_file_identity(
                                artifact_path, descriptor, "coverage binary"
                            )
                            cycle_retained.add(artifact_path.resolve())
                            continue
                        present = self._add_file_candidate(
                            cycle_candidates,
                            artifact_path,
                            "build_artifact",
                            descriptor,
                        )
                        if not present and (
                            baseline is None or role not in baseline.removed_artifacts
                        ):
                            raise RuntimeCleanupError(
                                "cleanup_candidate_unproven",
                                "Build artifact deletion identity could not be proven",
                            )
                    for input_run, run in zip(input_runs, selected_runs):
                        run_id = str(input_run["run_id"])
                        run_dir = self.paths.data_dir / "cycles" / cycle_id / "runs" / run_id
                        present = self._add_file_candidate(
                            cycle_candidates,
                            run_dir / "payload.profraw",
                            "run_profraw",
                            {
                                "size": input_run["payload_size"],
                                "sha256": input_run["payload_sha256"],
                            },
                        )
                        if not present and (
                            baseline is None
                            or not self.evidence_store.run_matches(baseline, run)
                        ):
                            raise RuntimeCleanupError(
                                "cleanup_candidate_unproven",
                                "Run profile deletion identity could not be proven",
                            )
                        cycle_retained.add((run_dir / "metadata.json").resolve())
                        cycle_retained.add((run_dir / "upload.sha256").resolve())
                current_input_sha = input_by_build.get(current_build_id)
                if current_input_sha is None:
                    raise RuntimeCleanupError(
                        "latest_success_invalid", "current Build single-Build result is missing"
                    )
                diff_dir = self._validate_diff_reference(
                    results.get("current_diff_result"), cycle_id,
                    current_build_id, current_input_sha,
                )
                cycle_dir = self._validate_cycle_reference(
                    results.get("cycle_coverage_result"), cycle_id,
                    current_build_id, cycle_input_sha,
                )
                self._protect_reference(cycle_retained, diff_dir)
                self._protect_reference(cycle_retained, cycle_dir)
                retained.update(cycle_retained)
                candidates.update(cycle_candidates)
                evidence_specs.extend(cycle_evidence)
                eligible_builds.update(cycle_builds)
                eligible_cycles.add(cycle_id)
            except (CoverageReportError, RetainedEvidenceError, RuntimeCleanupError, KeyError, TypeError) as error:
                skipped.append({
                    "cycle_id": cycle_id,
                    "reason": getattr(error, "code", "reference_unproven"),
                })
                continue

        protected_dirs = {path for path in retained if path.is_dir()}
        for generation in generations:
            result_dir = generation.path
            resolved = result_dir.resolve()
            if resolved not in protected_dirs and self._generation_is_eligible(
                generation, eligible_cycles, eligible_builds
            ):
                self._add_directory_candidate(candidates, result_dir, "analysis_generation")

        for evidence in self.evidence_store.valid_evidence():
            evidence_dir = self.evidence_store.evidence_directory(
                evidence.cycle_id, evidence.build_id, evidence.input_set_sha256
            ).resolve()
            if (
                evidence_dir not in retained
                and evidence.cycle_id in eligible_cycles
                and evidence.build_id in eligible_builds
            ):
                self._add_directory_candidate(
                    candidates, evidence_dir, "retained_evidence"
                )

        if unsafe_links:
            skipped.append({"reason": "unsafe_runtime_symlink", "count": len(unsafe_links)})
        planned = sorted(candidates.values(), key=self._deletion_order)
        deleted: List[CleanupCandidate] = []
        if apply:
            self._assert_no_active_analysis()
            with SecureRuntimeDeletion(self.root) as secure:
                self._assert_candidates_unchanged(planned, secure)
                for cycle_id, build_id, result_dir, removed_roles in evidence_specs:
                    self.evidence_store.publish(cycle_id, build_id, result_dir, removed_roles)
                self._assert_candidates_unchanged(planned, secure)
                for item in planned:
                    relative = self._relative_target(item.path)
                    if item.sha256 is not None:
                        secure.delete_file(relative, item.size, item.sha256)
                    else:
                        secure.delete_tree(relative, item.tree_entries)
                    deleted.append(item)

        bytes_after = self._managed_bytes()
        return self._summary(
            snapshot, generation_count, planned, deleted, skipped, not apply,
            len(retained), len(evidence_specs), bytes_before, bytes_after,
        )

    def _assert_no_active_analysis(self) -> None:
        """Rechecks scheduler state immediately before evidence publication and deletion."""

        snapshot = self._snapshot()
        if any(state["status"] in ("queued", "running") for state in snapshot["states"]):
            raise RuntimeCleanupError("analysis_active", "queued or running analysis prevents cleanup")

    def _snapshot(self) -> Dict[str, List[Dict[str, object]]]:
        """Reads lifecycle identities without changing the database or its schema."""

        uri = "file:" + str(self.paths.database.resolve()) + "?mode=ro"
        connection = sqlite3.connect(uri, uri=True, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only = ON")
        try:
            connection.execute("BEGIN")
            result = {
                "cycles": [dict(row) for row in connection.execute("SELECT * FROM coverage_cycles ORDER BY id")],
                "builds": [dict(row) for row in connection.execute("SELECT * FROM builds ORDER BY id")],
                "runs": [dict(row) for row in connection.execute("SELECT * FROM runs ORDER BY id")],
                "states": [dict(row) for row in connection.execute("SELECT * FROM cycle_analysis_states ORDER BY cycle_id")],
            }
            connection.execute("COMMIT")
            return result
        except sqlite3.Error as error:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            raise RuntimeCleanupError("database_read_failed", "coverage lifecycle snapshot could not be read") from error
        finally:
            connection.close()

    def _completed_generations(self) -> Iterable[CompletedGeneration]:
        """Yields only completion-valid results whose semantic identity matches their layout."""

        roots = (
            (self.paths.analysis_dir / "single-build", "completion.json"),
            (self.paths.analysis_dir / "single-build-diff", "completion.json"),
            (self.paths.analysis_dir / "cycle-coverage", "complete.json"),
            (self.paths.analysis_dir / "cycle-latest", "complete.json"),
        )
        for root, marker in roots:
            if not root.is_dir() or root.is_symlink():
                continue
            for completion_path in root.rglob(marker):
                directory = completion_path.parent
                try:
                    yield self._validate_completed_generation(directory, root.name)
                except (RuntimeCleanupError, RetainedEvidenceError):
                    continue

    def _validate_completed_directory(
        self, directory: Path, marker: str, identity_field: str, identity_sha256: str
    ) -> None:
        completion = self._load_json(directory / marker)
        outputs = completion.get("outputs")
        if (
            completion.get("schema_version") != 1
            or completion.get("status") != "complete"
            or completion.get(identity_field) != identity_sha256
            or not isinstance(outputs, dict)
        ):
            raise RuntimeCleanupError("analysis_generation_unproven", "analysis completion is invalid")
        actual = {p.relative_to(directory).as_posix() for p in directory.rglob("*") if p.is_file() and p.name != marker}
        if actual != set(outputs):
            raise RuntimeCleanupError("analysis_generation_unproven", "analysis output set changed")
        for relative, descriptor in outputs.items():
            safe = self._safe_relative(relative)
            if not isinstance(descriptor, dict) or self._identity(directory / safe) != descriptor:
                raise RuntimeCleanupError("analysis_generation_unproven", "analysis output bytes changed")

    def _validate_build_snapshot(
        self,
        build: Optional[Mapping[str, object]],
        inputs: Mapping[str, object],
        cycle_id: str,
        build_id: str,
    ) -> None:
        """Binds one successful single-Build result back to the current SQLite Build."""

        if (
            build is None
            or build.get("id") != build_id
            or build.get("cycle_id") != cycle_id
            or build.get("artifact_state") != "ready"
        ):
            raise RuntimeCleanupError("latest_success_invalid", "referenced Build is missing or not ready")
        input_build = inputs.get("build")
        if not isinstance(input_build, dict):
            raise RuntimeCleanupError("latest_success_invalid", "single-Build identity is invalid")
        manifest = input_build.get("manifest")
        if not isinstance(manifest, dict):
            raise RuntimeCleanupError("latest_success_invalid", "single-Build manifest is invalid")
        try:
            database_manifest = json.loads(str(build["manifest_json"]))
        except (KeyError, TypeError, ValueError) as error:
            raise RuntimeCleanupError("latest_success_invalid", "stored Build manifest is invalid") from error
        manifest_sha = hashlib.sha256(self._canonical_json_bytes(manifest)).hexdigest()
        disk_manifest = self._load_json(
            self.paths.data_dir / "cycles" / cycle_id / "builds" / build_id / "manifest.json"
        )
        if (
            database_manifest != manifest
            or disk_manifest != manifest
            or build.get("manifest_sha256") != manifest_sha
            or input_build.get("manifest_sha256") != manifest_sha
            or build.get("coverage_binary") != input_build.get("coverage_binary")
        ):
            raise RuntimeCleanupError("latest_success_invalid", "Build manifest identity changed")

    def _validate_run_snapshot(
        self,
        input_run: object,
        runs: Mapping[str, Mapping[str, object]],
        cycle_id: str,
        build_id: str,
    ) -> Mapping[str, object]:
        """Binds one incorporated Run to its current immutable SQLite and metadata identity."""

        if not isinstance(input_run, dict):
            raise RuntimeCleanupError("latest_success_invalid", "single-Build Run identity is invalid")
        run_id = self._canonical_uuid(input_run.get("run_id"), "single-Build Run")
        run = runs.get(run_id)
        if (
            run is None
            or run.get("id") != run_id
            or run.get("cycle_id") != cycle_id
            or run.get("build_id") != build_id
            or run.get("payload_size") != input_run.get("payload_size")
            or run.get("payload_sha256") != input_run.get("payload_sha256")
            or run.get("metadata_sha256") != input_run.get("metadata_sha256")
            or run.get("state") != "uploaded"
            or run.get("analyzable") != 1
            or run.get("error_message") is not None
        ):
            raise RuntimeCleanupError("latest_success_invalid", "incorporated Run identity changed")
        run_dir = self.paths.data_dir / "cycles" / cycle_id / "runs" / run_id
        metadata = self._load_json(run_dir / "metadata.json")
        if hashlib.sha256(self._canonical_json_bytes(metadata)).hexdigest() != run.get(
            "metadata_sha256"
        ):
            raise RuntimeCleanupError("latest_success_invalid", "stored Run metadata identity changed")
        upload_path = run_dir / "upload.sha256"
        if upload_path.is_symlink():
            raise RuntimeCleanupError("latest_success_invalid", "stored Run upload identity is unsafe")
        try:
            uploaded_sha = upload_path.read_text(encoding="ascii").strip()
        except (OSError, UnicodeError) as error:
            raise RuntimeCleanupError("latest_success_invalid", "stored Run upload identity is missing") from error
        if uploaded_sha != run.get("payload_sha256"):
            raise RuntimeCleanupError("latest_success_invalid", "stored Run upload identity changed")
        return run

    def _protect_reference(self, retained: Set[Path], directory: Path) -> None:
        self._assert_safe_target(directory)
        retained.add(directory.resolve())

    def _exact_reference_directory(
        self, reference: Mapping[str, object], expected: Path
    ) -> Path:
        """Requires a downstream pointer to equal its identity-derived fixed directory."""

        value = reference.get("output_directory")
        if not isinstance(value, str) or Path(value).resolve() != expected.resolve():
            raise RuntimeCleanupError("latest_success_invalid", "result output directory is invalid")
        self._assert_safe_target(expected)
        return expected.resolve()

    def _validate_reference_files(
        self, reference: Mapping[str, object], directory: Path, completion_name: str
    ) -> None:
        """Verifies exact completion and summary bytes declared by an orchestration."""

        for key, filename in (("completion", completion_name), ("summary", "summary.json")):
            descriptor = reference.get(key)
            if not isinstance(descriptor, dict) or self._identity(directory / filename) != descriptor:
                raise RuntimeCleanupError("latest_success_invalid", "latest success result bytes changed")

    def _validate_orchestration_generation(
        self,
        directory: Path,
        cycle_id: str,
        orchestration_sha: str,
        current_build_id: str,
        cycle_input_sha: str,
        state: Mapping[str, object],
    ) -> Dict[str, object]:
        """Validates the latest orchestration as its exact result kind and persisted state."""

        generation = self._validate_completed_generation(directory, "cycle-latest")
        declared = state.get("latest_success_output_directory")
        if (
            generation.cycle_id != cycle_id
            or generation.build_id != current_build_id
            or generation.identity_sha256 != orchestration_sha
            or not isinstance(declared, str)
            or Path(declared).resolve() != directory.resolve()
        ):
            raise RuntimeCleanupError("latest_success_invalid", "latest orchestration identity changed")
        summary = self._load_json(directory / "summary.json")
        if summary.get("cycle_input_set_sha256") != cycle_input_sha:
            raise RuntimeCleanupError("latest_success_invalid", "latest Cycle input identity changed")
        results = self._load_json(directory / "results.json")
        if results.get("schema_version") != 1:
            raise RuntimeCleanupError("latest_success_invalid", "latest result references are invalid")
        return results

    def _validate_diff_reference(
        self, reference: object, cycle_id: str, build_id: str, input_sha: str
    ) -> Path:
        """Requires current_diff_result to be the exact current Build diff generation."""

        if not isinstance(reference, dict):
            raise RuntimeCleanupError("latest_success_invalid", "current diff reference is invalid")
        diff_sha = self._canonical_sha256(reference.get("diff_set_sha256"), "diff result")
        expected = (
            self.paths.analysis_dir / "single-build-diff" / build_id / input_sha / diff_sha
        )
        directory = self._exact_reference_directory(reference, expected)
        self._validate_reference_files(reference, directory, "completion.json")
        generation = self._validate_completed_generation(directory, "single-build-diff")
        if (
            generation.cycle_id != cycle_id
            or generation.build_id != build_id
            or generation.identity_sha256 != diff_sha
        ):
            raise RuntimeCleanupError("latest_success_invalid", "current diff identity changed")
        return directory

    def _validate_cycle_reference(
        self, reference: object, cycle_id: str, build_id: str, cycle_input_sha: str
    ) -> Path:
        """Requires cycle_coverage_result to be the exact latest cumulative generation."""

        if not isinstance(reference, dict):
            raise RuntimeCleanupError("latest_success_invalid", "Cycle result reference is invalid")
        if reference.get("cycle_input_set_sha256") != cycle_input_sha:
            raise RuntimeCleanupError("latest_success_invalid", "Cycle result identity changed")
        expected = (
            self.paths.analysis_dir / "cycle-coverage" / cycle_id / build_id / cycle_input_sha
        )
        directory = self._exact_reference_directory(reference, expected)
        self._validate_reference_files(reference, directory, "complete.json")
        generation = self._validate_completed_generation(directory, "cycle-coverage")
        if (
            generation.cycle_id != cycle_id
            or generation.build_id != build_id
            or generation.identity_sha256 != cycle_input_sha
        ):
            raise RuntimeCleanupError("latest_success_invalid", "Cycle result identity changed")
        return directory

    def _validate_completed_generation(
        self, directory: Path, kind: str
    ) -> CompletedGeneration:
        """Validates one result kind, fixed path depth, and internal semantic identities."""

        roots = {
            "single-build": (self.paths.analysis_dir / "single-build", 2),
            "single-build-diff": (self.paths.analysis_dir / "single-build-diff", 3),
            "cycle-coverage": (self.paths.analysis_dir / "cycle-coverage", 3),
            "cycle-latest": (self.paths.analysis_dir / "cycle-latest", 2),
        }
        if kind not in roots:
            raise RuntimeCleanupError("analysis_generation_unproven", "analysis result kind is invalid")
        root, depth = roots[kind]
        candidate = Path(directory)
        if candidate.is_symlink():
            raise RuntimeCleanupError("analysis_generation_unproven", "analysis result path is unsafe")
        resolved = candidate.resolve()
        try:
            parts = resolved.relative_to(root.resolve()).parts
        except ValueError as error:
            raise RuntimeCleanupError("analysis_generation_unproven", "analysis result escaped its root") from error
        if len(parts) != depth:
            raise RuntimeCleanupError("analysis_generation_unproven", "analysis result layout is invalid")

        if kind == "single-build":
            build_id = self._canonical_uuid(parts[0], "single-Build path")
            identity = self._canonical_sha256(parts[1], "single-Build path")
            inputs = self._load_json(resolved / "inputs.json")
            cycle_id = self._canonical_uuid(inputs.get("cycle_id"), "single-Build Cycle")
            self.evidence_store.validate_result(resolved, cycle_id, build_id)
            return CompletedGeneration(resolved, kind, cycle_id, build_id, identity)

        if kind == "single-build-diff":
            build_id = self._canonical_uuid(parts[0], "diff Build")
            input_sha = self._canonical_sha256(parts[1], "diff input")
            identity = self._canonical_sha256(parts[2], "diff identity")
            self._validate_completed_directory(
                resolved, "completion.json", "diff_set_sha256", identity
            )
            inputs = self._load_json(resolved / "inputs.json")
            summary = self._load_json(resolved / "summary.json")
            cycle_id = self._canonical_uuid(inputs.get("cycle_id"), "diff Cycle")
            if not self._documents_match(
                (inputs, summary),
                {"schema_version": 1, "cycle_id": cycle_id, "build_id": build_id,
                 "input_set_sha256": input_sha, "diff_set_sha256": identity},
            ) or summary.get("status") != "complete":
                raise RuntimeCleanupError("analysis_generation_unproven", "diff result identity changed")
            return CompletedGeneration(resolved, kind, cycle_id, build_id, identity)

        if kind == "cycle-coverage":
            cycle_id = self._canonical_uuid(parts[0], "Cycle result Cycle")
            build_id = self._canonical_uuid(parts[1], "Cycle result Build")
            identity = self._canonical_sha256(parts[2], "Cycle result identity")
            self._validate_completed_directory(
                resolved, "complete.json", "cycle_input_set_sha256", identity
            )
            manifest = self._load_json(resolved / "manifest.json")
            summary = self._load_json(resolved / "summary.json")
            if (
                manifest.get("schema_version") != 1
                or manifest.get("cycle_id") != cycle_id
                or manifest.get("cycle_input_set_sha256") != identity
                or not isinstance(manifest.get("current_build"), dict)
                or manifest["current_build"].get("build_id") != build_id
                or not self._documents_match(
                    (summary,),
                    {"schema_version": 1, "status": "complete", "cycle_id": cycle_id,
                     "current_build_id": build_id, "cycle_input_set_sha256": identity},
                )
            ):
                raise RuntimeCleanupError("analysis_generation_unproven", "Cycle result identity changed")
            return CompletedGeneration(resolved, kind, cycle_id, build_id, identity)

        cycle_id = self._canonical_uuid(parts[0], "orchestration Cycle")
        identity = self._canonical_sha256(parts[1], "orchestration identity")
        self._validate_completed_directory(
            resolved, "complete.json", "orchestration_input_sha256", identity
        )
        manifest = self._load_json(resolved / "manifest.json")
        summary = self._load_json(resolved / "summary.json")
        cycle = manifest.get("cycle")
        current_build_id = self._canonical_uuid(
            manifest.get("current_build_id"), "orchestration current Build"
        )
        if (
            manifest.get("schema_version") != 1
            or not isinstance(cycle, dict)
            or cycle.get("cycle_id") != cycle_id
            or not self._documents_match(
                (summary,),
                {"schema_version": 1, "status": "complete", "cycle_id": cycle_id,
                 "current_build_id": current_build_id,
                 "orchestration_input_sha256": identity},
            )
        ):
            raise RuntimeCleanupError("analysis_generation_unproven", "orchestration identity changed")
        results = self._load_json(resolved / "results.json")
        single_references = results.get("single_build_results")
        if results.get("schema_version") != 1 or not isinstance(single_references, list):
            raise RuntimeCleanupError("analysis_generation_unproven", "orchestration references are invalid")
        input_by_build: Dict[str, str] = {}
        for reference in single_references:
            if not isinstance(reference, dict):
                raise RuntimeCleanupError("analysis_generation_unproven", "single-Build reference is invalid")
            referenced_build = self._canonical_uuid(reference.get("build_id"), "single-Build reference")
            input_sha = self._canonical_sha256(reference.get("input_set_sha256"), "single-Build input")
            if referenced_build in input_by_build:
                raise RuntimeCleanupError("analysis_generation_unproven", "single-Build reference is duplicated")
            input_by_build[referenced_build] = input_sha
            single_dir = self._exact_reference_directory(
                reference,
                self.paths.analysis_dir / "single-build" / referenced_build / input_sha,
            )
            self._validate_reference_files(reference, single_dir, "completion.json")
            single_generation = self._validate_completed_generation(single_dir, "single-build")
            if single_generation.cycle_id != cycle_id:
                raise RuntimeCleanupError("analysis_generation_unproven", "single-Build Cycle changed")
        current_input = input_by_build.get(current_build_id)
        if current_input is None:
            raise RuntimeCleanupError("analysis_generation_unproven", "current Build result is missing")
        self._validate_diff_reference(
            results.get("current_diff_result"), cycle_id, current_build_id, current_input
        )
        cycle_reference = results.get("cycle_coverage_result")
        if not isinstance(cycle_reference, dict):
            raise RuntimeCleanupError("analysis_generation_unproven", "Cycle result reference is invalid")
        cycle_sha = self._canonical_sha256(
            cycle_reference.get("cycle_input_set_sha256"), "Cycle result"
        )
        self._validate_cycle_reference(
            cycle_reference, cycle_id, current_build_id, cycle_sha
        )
        return CompletedGeneration(resolved, kind, cycle_id, current_build_id, identity)

    def _documents_match(
        self, documents: Sequence[Mapping[str, object]], expected: Mapping[str, object]
    ) -> bool:
        return all(all(document.get(key) == value for key, value in expected.items()) for document in documents)

    def _add_file_candidate(
        self,
        candidates: Dict[Path, CleanupCandidate],
        path: Path,
        category: str,
        expected: Mapping[str, object],
    ) -> bool:
        """Adds only an existing file whose current bytes equal the incorporated identity."""

        self._assert_safe_target(path)
        if not path.exists():
            return False
        self._assert_file_identity(path, expected, "cleanup candidate")
        size = expected.get("size")
        sha256 = expected.get("sha256")
        candidates[path.resolve()] = CleanupCandidate(
            path.resolve(), category, int(size), str(sha256)
        )
        return True

    def _assert_file_identity(
        self, path: Path, expected: Mapping[str, object], label: str
    ) -> None:
        """Requires one current regular file to equal an exact size/SHA identity."""

        size = expected.get("size")
        sha256 = expected.get("sha256")
        if (
            not isinstance(size, int)
            or isinstance(size, bool)
            or size < 0
            or not isinstance(sha256, str)
            or len(sha256) != 64
            or any(character not in "0123456789abcdef" for character in sha256)
        ):
            raise RuntimeCleanupError(
                "cleanup_candidate_unproven", label + " expected identity is invalid"
            )
        try:
            actual = self._identity(path)
        except RuntimeCleanupError as error:
            raise RuntimeCleanupError(
                "cleanup_candidate_unproven", label + " identity could not be proven"
            ) from error
        if actual != {"size": size, "sha256": sha256}:
            raise RuntimeCleanupError(
                "cleanup_candidate_identity_mismatch", label + " bytes changed"
            )

    def _assert_candidates_unchanged(
        self,
        candidates: Sequence[CleanupCandidate],
        secure: SecureRuntimeDeletion,
    ) -> None:
        """Preflights every file and tree around retained-evidence publication."""

        for candidate in candidates:
            if candidate.sha256 is not None:
                secure.verify_file(
                    self._relative_target(candidate.path), candidate.size, candidate.sha256
                )
            else:
                secure.verify_tree(
                    self._relative_target(candidate.path), candidate.tree_entries
                )

    def _relative_target(self, path: Path) -> Path:
        """Converts a planned lexical target to a descriptor-relative runtime path."""

        try:
            return path.relative_to(self.root)
        except ValueError as error:
            raise RuntimeCleanupError(
                "cleanup_path_escaped", "cleanup target escaped runtime_root"
            ) from error

    def _deletion_order(self, candidate: CleanupCandidate) -> Tuple[int, str]:
        """Removes obsolete retained pointers before the old results they reference."""

        priority = 0 if candidate.category == "retained_evidence" else 1
        return priority, str(candidate.path)

    def _add_directory_candidate(self, candidates: Dict[Path, CleanupCandidate], path: Path, category: str) -> None:
        self._assert_safe_target(path)
        if path.is_dir() and not path.is_symlink():
            entries = self._directory_tree_identity(path)
            if entries is None:
                return
            size = sum(entry[2] for entry in entries if entry[1] == "file")
            candidates[path.resolve()] = CleanupCandidate(
                path.resolve(), category, size, tree_entries=entries
            )

    def _directory_tree_identity(self, path: Path) -> Optional[Tuple[TreeEntry, ...]]:
        """Captures every regular file and directory in one deletion candidate tree."""

        entries: List[TreeEntry] = []
        try:
            descendants = sorted(
                path.rglob("*"), key=lambda item: item.relative_to(path).as_posix()
            )
            for item in descendants:
                if item.is_symlink():
                    return None
                relative = item.relative_to(path).as_posix()
                if item.is_dir():
                    entries.append((relative, "directory", 0, ""))
                elif item.is_file():
                    identity = self._identity(item)
                    entries.append(
                        (relative, "file", identity["size"], identity["sha256"])
                    )
                else:
                    return None
        except OSError:
            return None
        return tuple(entries)

    def _assert_safe_target(self, path: Path) -> None:
        try:
            path.resolve().relative_to(self.root)
        except ValueError as error:
            raise RuntimeCleanupError("cleanup_path_escaped", "cleanup target escaped runtime_root") from error

    def _unsafe_symlinks(self) -> List[Path]:
        result = []
        for root in (self.paths.data_dir, self.paths.analysis_dir):
            if not root.exists():
                continue
            for current, directories, files in os.walk(str(root), followlinks=False):
                for name in directories + files:
                    candidate = Path(current) / name
                    if candidate.is_symlink():
                        result.append(candidate)
        return result

    def _generation_is_eligible(
        self, generation: CompletedGeneration, cycle_ids: Set[str], build_ids: Set[str]
    ) -> bool:
        """Limits old-generation deletion to its semantically proven Cycle and Build."""

        if generation.cycle_id not in cycle_ids:
            return False
        if generation.kind.startswith("single-build"):
            return generation.build_id in build_ids
        return True

    def _summary(
        self, snapshot: Mapping[str, Sequence[Mapping[str, object]]], generations: int,
        planned: Sequence[CleanupCandidate], deleted: Sequence[CleanupCandidate],
        skipped: Sequence[Mapping[str, object]], dry_run: bool,
        retained_paths: int, retained_build_baselines: int,
        bytes_before: int, bytes_after: int,
    ) -> Dict[str, object]:
        def grouped(items: Sequence[CleanupCandidate]) -> Dict[str, Dict[str, int]]:
            result: Dict[str, Dict[str, int]] = {}
            for item in items:
                value = result.setdefault(item.category, {"items": 0, "bytes": 0})
                value["items"] += 1
                value["bytes"] += item.size
            return result
        return {
            "schema_version": 1,
            "status": "complete",
            "dry_run": dry_run,
            "scanned": {
                "cycles": len(snapshot["cycles"]), "builds": len(snapshot["builds"]),
                "runs": len(snapshot["runs"]), "analysis_generations": generations,
            },
            "retained": {
                "protected_paths": retained_paths,
                "build_baselines": retained_build_baselines,
            },
            "planned_deletion": grouped(planned),
            "actual_deletion": grouped(deleted),
            "planned_release_bytes": sum(item.size for item in planned),
            "released_bytes": sum(item.size for item in deleted),
            "managed_bytes_before": bytes_before,
            "managed_bytes_after": bytes_after,
            "skipped_count": len(skipped),
            "skipped": list(skipped),
        }

    def _managed_bytes(self) -> int:
        """Counts regular data/analysis bytes without following symbolic links."""

        total = 0
        for root in (self.paths.data_dir, self.paths.analysis_dir):
            if not root.is_dir() or root.is_symlink():
                continue
            for current, directories, files in os.walk(str(root), followlinks=False):
                directories[:] = [name for name in directories if not (Path(current) / name).is_symlink()]
                for name in files:
                    path = Path(current) / name
                    if path.is_file() and not path.is_symlink():
                        total += path.stat().st_size
        return total

    def _identity(self, path: Path) -> Dict[str, object]:
        if not path.is_file() or path.is_symlink():
            raise RuntimeCleanupError("file_identity_unproven", "referenced file is missing or unsafe")
        digest = hashlib.sha256()
        size = 0
        with path.open("rb") as source:
            for chunk in iter(lambda: source.read(1024 * 1024), b""):
                size += len(chunk)
                digest.update(chunk)
        return {"size": size, "sha256": digest.hexdigest()}

    def _canonical_json_bytes(self, value: Mapping[str, object]) -> bytes:
        """Matches the storage-layer semantic JSON identity without sharing mutable state."""

        return json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")

    def _load_json(self, path: Path) -> Dict[str, object]:
        if path.is_symlink():
            raise RuntimeCleanupError("json_unsafe", "referenced JSON is unsafe")
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, ValueError) as error:
            raise RuntimeCleanupError("json_invalid", "referenced JSON is missing or invalid") from error
        if not isinstance(value, dict):
            raise RuntimeCleanupError("json_invalid", "referenced JSON must be an object")
        return value

    def _safe_relative(self, value: object) -> Path:
        if not isinstance(value, str) or not value:
            raise RuntimeCleanupError("completion_path_invalid", "completion path is invalid")
        path = PurePosixPath(value)
        if path.is_absolute() or path.as_posix() != value or ".." in path.parts:
            raise RuntimeCleanupError("completion_path_invalid", "completion path is unsafe")
        return Path(*path.parts)

    def _canonical_uuid(self, value: object, field: str) -> str:
        try:
            normalized = str(uuid.UUID(str(value)))
        except (ValueError, TypeError, AttributeError) as error:
            raise RuntimeCleanupError("latest_success_invalid", field + " identity is invalid") from error
        if normalized != value:
            raise RuntimeCleanupError("latest_success_invalid", field + " identity is not canonical")
        return normalized

    def _canonical_sha256(self, value: object, field: str) -> str:
        if not isinstance(value, str) or len(value) != 64 or any(
            character not in "0123456789abcdef" for character in value
        ):
            raise RuntimeCleanupError("latest_success_invalid", field + " identity is invalid")
        return value
