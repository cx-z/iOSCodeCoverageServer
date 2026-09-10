"""Completion-protected evidence that permits safe runtime input compaction."""

import hashlib
import json
import os
import shutil
import tempfile
import uuid
import re
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Dict, Mapping, Optional, Sequence, Set, Tuple


RETAINED_EVIDENCE_SCHEMA_VERSION = 1
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")


class RetainedEvidenceError(ValueError):
    """Reports an incomplete or unsafe retained Build baseline."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class RetainedBuildEvidence:
    """A validated same-Build profile baseline and its compacted input identities."""

    cycle_id: str
    build_id: str
    input_set_sha256: str
    manifest_sha256: str
    result_directory: Path
    merged_profdata: Path
    llvm_cov_export: Path
    run_identities: Mapping[str, Mapping[str, object]]
    removed_artifacts: Set[str]
    actual_macho_uuids: Mapping[str, Sequence[str]]


class RetainedBuildEvidenceStore:
    """Publishes and validates small pointers to immutable single-Build results."""

    def __init__(self, analysis_root: Path):
        self.analysis_root = analysis_root.resolve()
        self.single_build_root = self.analysis_root / "single-build"
        self.evidence_root = self.analysis_root / "retained-build"

    def evidence_directory(self, cycle_id: str, build_id: str, input_set_sha256: str) -> Path:
        """Returns the fixed evidence path after validating every identity component."""

        cycle = self._canonical_uuid(cycle_id, "cycle_id")
        build = self._canonical_uuid(build_id, "build_id")
        if not SHA256_PATTERN.fullmatch(str(input_set_sha256)):
            raise RetainedEvidenceError("retained_evidence_identity_invalid", "input set identity is invalid")
        return self.evidence_root / cycle / build / str(input_set_sha256)

    def valid_evidence(self) -> Sequence[RetainedBuildEvidence]:
        """Returns only fully validated evidence records for conservative cleanup planning."""

        if not self.evidence_root.is_dir() or self.evidence_root.is_symlink():
            return ()
        records = []
        for completion in self.evidence_root.glob("*/*/*/complete.json"):
            try:
                records.append(self._load_directory(completion.parent))
            except RetainedEvidenceError:
                continue
        return tuple(records)

    def publish(
        self,
        cycle_id: str,
        build_id: str,
        result_directory: Path,
        removed_artifacts: Sequence[str],
    ) -> RetainedBuildEvidence:
        """Atomically records one fully validated baseline before any input deletion."""

        cycle_id = self._canonical_uuid(cycle_id, "cycle_id")
        build_id = self._canonical_uuid(build_id, "build_id")
        result = self._validate_single_build_result(result_directory, cycle_id, build_id)
        inputs = result["inputs"]
        input_sha = str(inputs["input_set_sha256"])
        if not SHA256_PATTERN.fullmatch(input_sha):
            raise RetainedEvidenceError("retained_evidence_invalid", "input set identity is invalid")
        relative_result = result_directory.resolve().relative_to(self.analysis_root).as_posix()
        scoped_runs = [self._scoped_run_identity(cycle_id, build_id, item) for item in inputs["runs"]]
        manifest = {
            "schema_version": RETAINED_EVIDENCE_SCHEMA_VERSION,
            "cycle_id": cycle_id,
            "build_id": build_id,
            "input_set_sha256": input_sha,
            "manifest_sha256": inputs["build"]["manifest_sha256"],
            "coverage_binary": inputs["build"]["coverage_binary"],
            "actual_macho_uuids": inputs["build"]["actual_macho_uuids"],
            "runs": scoped_runs,
            "removed_artifacts": sorted(set(removed_artifacts)),
            "single_build_result": {
                "relative_directory": relative_result,
                "completion": self._identity(result_directory / "completion.json"),
                "inputs": self._identity(result_directory / "inputs.json"),
                "merged_profdata": self._identity(result_directory / "merged.profdata"),
                "llvm_cov_export": self._identity(result_directory / "llvm-cov-export.json"),
                "summary": self._identity(result_directory / "summary.json"),
            },
        }
        destination = self.evidence_directory(cycle_id, build_id, input_sha)
        if destination.exists():
            evidence = self._load_directory(destination)
            if self._load_json(destination / "manifest.json") != manifest:
                raise RetainedEvidenceError(
                    "retained_evidence_conflict", "retained Build evidence conflicts with existing bytes"
                )
            self._fsync_publication_chain(destination)
            return evidence
        self._ensure_directory_tree(destination.parent)
        staging_parent = self.evidence_root / ".staging"
        self._ensure_directory_tree(staging_parent)
        staging = Path(tempfile.mkdtemp(prefix=".retained-", dir=str(staging_parent)))
        try:
            self._write_json(staging / "manifest.json", manifest)
            completion = {
                "schema_version": RETAINED_EVIDENCE_SCHEMA_VERSION,
                "status": "complete",
                "input_set_sha256": input_sha,
                "outputs": {"manifest.json": self._identity(staging / "manifest.json")},
            }
            self._write_json(staging / "complete.json", completion)
            self._fsync_directory(staging)
            os.rename(str(staging), str(destination))
            self._fsync_publication_chain(destination)
        finally:
            if staging.exists():
                shutil.rmtree(str(staging), ignore_errors=True)
        return self._load_directory(destination)

    def validate_result(
        self, result_directory: Path, cycle_id: str, build_id: str
    ) -> Mapping[str, object]:
        """Exposes read-only result validation for cleanup planning."""

        return self._validate_single_build_result(result_directory, cycle_id, build_id)

    def find_compatible(
        self,
        cycle_id: str,
        build_id: str,
        manifest_sha256: object,
        runs: Sequence[Mapping[str, object]],
    ) -> Optional[RetainedBuildEvidence]:
        """Returns the largest intact baseline whose Runs are an exact subset."""

        cycle_id = self._canonical_uuid(cycle_id, "cycle_id")
        build_id = self._canonical_uuid(build_id, "build_id")
        parent = self.evidence_root / cycle_id / build_id
        if not parent.is_dir() or parent.is_symlink():
            return None
        selected = {
            str(run["id"]): self._run_identity(run, cycle_id, build_id) for run in runs
        }
        candidates = []
        for path in parent.iterdir():
            if not path.is_dir() or path.is_symlink():
                continue
            try:
                evidence = self._load_directory(path)
            except RetainedEvidenceError:
                continue
            if evidence.cycle_id != cycle_id or evidence.build_id != build_id:
                continue
            if evidence.manifest_sha256 != manifest_sha256:
                continue
            if all(
                selected.get(run_id) == identity
                for run_id, identity in evidence.run_identities.items()
            ):
                candidates.append(evidence)
        return max(candidates, key=lambda item: (len(item.run_identities), item.input_set_sha256), default=None)

    def run_is_retained(self, cycle_id: str, build_id: str, run: Mapping[str, object]) -> bool:
        """Checks whether a missing Run payload has an intact exact baseline."""

        cycle_id = self._canonical_uuid(cycle_id, "cycle_id")
        build_id = self._canonical_uuid(build_id, "build_id")
        parent = self.evidence_root / cycle_id / build_id
        if not parent.is_dir():
            return False
        expected = self._run_identity(run, cycle_id, build_id)
        for path in parent.iterdir():
            try:
                loaded = self._load_directory(path)
            except (RetainedEvidenceError, OSError):
                continue
            if (
                loaded.cycle_id == cycle_id
                and loaded.build_id == build_id
                and loaded.run_identities.get(str(run["id"])) == expected
            ):
                return True
        return False

    def run_matches(self, evidence: RetainedBuildEvidence, run: Mapping[str, object]) -> bool:
        """Checks one selected SQLite Run against an already validated Build baseline."""

        expected = self._run_identity(run, evidence.cycle_id, evidence.build_id)
        return evidence.run_identities.get(str(run["id"])) == expected

    def build_artifact_is_retained(
        self, cycle_id: str, build_id: str, manifest_sha256: object, role: str
    ) -> bool:
        """Checks whether one intentionally removed artifact is covered by evidence."""

        cycle_id = self._canonical_uuid(cycle_id, "cycle_id")
        build_id = self._canonical_uuid(build_id, "build_id")
        parent = self.evidence_root / cycle_id / build_id
        if not parent.is_dir():
            return False
        for path in parent.iterdir():
            try:
                evidence = self._load_directory(path)
            except (RetainedEvidenceError, OSError):
                continue
            if evidence.manifest_sha256 == manifest_sha256 and role in evidence.removed_artifacts:
                return True
        return False

    def _load_directory(self, directory: Path) -> RetainedBuildEvidence:
        """Validates the evidence completion and every referenced baseline byte."""

        candidate = Path(directory)
        if candidate.is_symlink():
            raise RetainedEvidenceError("retained_evidence_path_invalid", "retained evidence path is unsafe")
        directory = candidate.resolve()
        try:
            parts = directory.relative_to(self.evidence_root).parts
        except ValueError as error:
            raise RetainedEvidenceError("retained_evidence_path_invalid", "retained evidence escaped its root") from error
        if len(parts) != 3:
            raise RetainedEvidenceError("retained_evidence_path_invalid", "retained evidence layout is invalid")
        path_cycle = self._canonical_uuid(parts[0], "cycle_id")
        path_build = self._canonical_uuid(parts[1], "build_id")
        path_input = parts[2]
        if not SHA256_PATTERN.fullmatch(path_input):
            raise RetainedEvidenceError("retained_evidence_identity_invalid", "input set identity is invalid")
        if not directory.is_dir():
            raise RetainedEvidenceError("retained_evidence_missing", "retained evidence is missing")
        manifest = self._load_json(directory / "manifest.json")
        completion = self._load_json(directory / "complete.json")
        input_sha = manifest.get("input_set_sha256")
        if (
            manifest.get("schema_version") != RETAINED_EVIDENCE_SCHEMA_VERSION
            or manifest.get("cycle_id") != path_cycle
            or manifest.get("build_id") != path_build
            or input_sha != path_input
            or completion.get("schema_version") != RETAINED_EVIDENCE_SCHEMA_VERSION
            or completion.get("status") != "complete"
            or completion.get("input_set_sha256") != input_sha
            or completion.get("outputs") != {"manifest.json": self._identity(directory / "manifest.json")}
        ):
            raise RetainedEvidenceError("retained_evidence_invalid", "retained evidence completion is invalid")
        reference = manifest.get("single_build_result")
        if not isinstance(reference, dict):
            raise RetainedEvidenceError("retained_evidence_invalid", "single-Build reference is invalid")
        relative = self._safe_relative(reference.get("relative_directory"))
        result_directory = (self.analysis_root / relative).resolve()
        try:
            result_directory.relative_to(self.single_build_root)
        except ValueError as error:
            raise RetainedEvidenceError("retained_evidence_path_invalid", "single-Build reference escaped its root") from error
        result = self._validate_single_build_result(
            result_directory, str(manifest.get("cycle_id")), str(manifest.get("build_id"))
        )
        for key, filename in (
            ("completion", "completion.json"), ("inputs", "inputs.json"),
            ("merged_profdata", "merged.profdata"), ("llvm_cov_export", "llvm-cov-export.json"),
            ("summary", "summary.json"),
        ):
            if reference.get(key) != self._identity(result_directory / filename):
                raise RetainedEvidenceError("retained_evidence_changed", "retained baseline bytes changed")
        inputs = result["inputs"]
        if (
            inputs.get("input_set_sha256") != input_sha
            or inputs.get("build", {}).get("manifest_sha256") != manifest.get("manifest_sha256")
        ):
            raise RetainedEvidenceError("retained_evidence_changed", "retained baseline identity changed")
        manifest_sha = manifest.get("manifest_sha256")
        coverage_binary = manifest.get("coverage_binary")
        actual_uuids = manifest.get("actual_macho_uuids")
        if (
            not isinstance(manifest_sha, str)
            or not SHA256_PATTERN.fullmatch(manifest_sha)
            or coverage_binary not in {"main_executable", "debug_dylib"}
            or not isinstance(actual_uuids, dict)
        ):
            raise RetainedEvidenceError("retained_evidence_invalid", "retained Build identity is invalid")
        expected_runs = [
            self._scoped_run_identity(path_cycle, path_build, item) for item in inputs["runs"]
        ]
        if (
            manifest.get("runs") != expected_runs
            or len({item["run_id"] for item in expected_runs}) != len(expected_runs)
        ):
            raise RetainedEvidenceError("retained_evidence_changed", "retained Run identities changed")
        run_identities = {str(item["run_id"]): dict(item) for item in expected_runs}
        removed = manifest.get("removed_artifacts")
        artifact_roles = set(inputs["build"]["manifest"]["artifacts"])
        if (
            not isinstance(removed, list)
            or len(set(removed)) != len(removed)
            or any(role not in artifact_roles for role in removed)
            or coverage_binary in removed
        ):
            raise RetainedEvidenceError("retained_evidence_invalid", "removed artifact roles are invalid")
        return RetainedBuildEvidence(
            cycle_id=path_cycle, build_id=path_build,
            input_set_sha256=str(input_sha), result_directory=result_directory,
            manifest_sha256=manifest_sha,
            merged_profdata=result_directory / "merged.profdata",
            llvm_cov_export=result_directory / "llvm-cov-export.json",
            run_identities=run_identities, removed_artifacts=set(removed),
            actual_macho_uuids=actual_uuids,
        )

    def _validate_single_build_result(self, directory: Path, cycle_id: str, build_id: str) -> Dict[str, object]:
        """Validates a complete immutable single-Build directory and core identity."""

        candidate = Path(directory)
        if candidate.is_symlink():
            raise RetainedEvidenceError("retained_result_path_invalid", "single-Build result path is unsafe")
        directory = candidate.resolve()
        try:
            parts = directory.relative_to(self.single_build_root).parts
        except ValueError as error:
            raise RetainedEvidenceError("retained_result_path_invalid", "single-Build result escaped its root") from error
        if len(parts) != 2 or parts[0] != build_id or not SHA256_PATTERN.fullmatch(parts[1]):
            raise RetainedEvidenceError("retained_result_path_invalid", "single-Build result layout is invalid")
        if not directory.is_dir():
            raise RetainedEvidenceError("retained_result_missing", "single-Build result is missing")
        inputs = self._load_json(directory / "inputs.json")
        summary = self._load_json(directory / "summary.json")
        completion = self._load_json(directory / "completion.json")
        if (
            inputs.get("schema_version") != RETAINED_EVIDENCE_SCHEMA_VERSION
            or inputs.get("cycle_id") != cycle_id
            or inputs.get("build_id") != build_id
            or inputs.get("input_set_sha256") != parts[1]
        ):
            raise RetainedEvidenceError("retained_result_identity_mismatch", "single-Build identity changed")
        if (
            completion.get("schema_version") != RETAINED_EVIDENCE_SCHEMA_VERSION
            or completion.get("status") != "complete"
            or completion.get("input_set_sha256") != parts[1]
            or summary.get("schema_version") != RETAINED_EVIDENCE_SCHEMA_VERSION
            or summary.get("status") != "complete"
            or summary.get("cycle_id") != cycle_id
            or summary.get("build_id") != build_id
            or summary.get("input_set_sha256") != parts[1]
        ):
            raise RetainedEvidenceError("retained_result_invalid", "single-Build completion is invalid")
        outputs = completion.get("outputs")
        if not isinstance(outputs, dict):
            raise RetainedEvidenceError("retained_result_invalid", "single-Build output identity is missing")
        actual = {p.relative_to(directory).as_posix() for p in directory.rglob("*") if p.is_file() and p.name != "completion.json"}
        if actual != set(outputs):
            raise RetainedEvidenceError("retained_result_changed", "single-Build output set changed")
        for relative, descriptor in outputs.items():
            safe = self._safe_relative(relative)
            if not isinstance(descriptor, dict) or descriptor != self._identity(directory / safe):
                raise RetainedEvidenceError("retained_result_changed", "single-Build output bytes changed")
        for filename in ("merged.profdata", "llvm-cov-export.json", "summary.json"):
            if not (directory / filename).is_file():
                raise RetainedEvidenceError("retained_result_invalid", "single-Build retained output is missing")
        return {"inputs": inputs, "completion": completion}

    def _run_identity(
        self, run: Mapping[str, object], cycle_id: str, build_id: str
    ) -> Dict[str, object]:
        run_id = self._canonical_uuid(run.get("id", run.get("run_id")), "run_id")
        payload_size = run.get("payload_size")
        payload_sha = run.get("payload_sha256")
        metadata_sha = run.get("metadata_sha256")
        if (
            not isinstance(payload_size, int)
            or isinstance(payload_size, bool)
            or payload_size <= 0
            or not isinstance(payload_sha, str)
            or not SHA256_PATTERN.fullmatch(payload_sha)
            or not isinstance(metadata_sha, str)
            or not SHA256_PATTERN.fullmatch(metadata_sha)
        ):
            raise RetainedEvidenceError("retained_evidence_identity_invalid", "Run identity is invalid")
        return {
            "run_id": run_id,
            "cycle_id": run.get("cycle_id", cycle_id),
            "build_id": run.get("build_id", build_id),
            "payload_size": payload_size, "payload_sha256": payload_sha,
            "metadata_sha256": metadata_sha,
        }

    def _scoped_run_identity(
        self, cycle_id: str, build_id: str, run: Mapping[str, object]
    ) -> Dict[str, object]:
        identity = self._run_identity(run, cycle_id, build_id)
        if identity["cycle_id"] != cycle_id or identity["build_id"] != build_id:
            raise RetainedEvidenceError("retained_evidence_identity_mismatch", "retained Run ownership changed")
        return identity

    def _safe_relative(self, value: object) -> Path:
        if not isinstance(value, str) or not value:
            raise RetainedEvidenceError("retained_evidence_path_invalid", "retained path is invalid")
        path = PurePosixPath(value)
        if path.is_absolute() or path.as_posix() != value or ".." in path.parts:
            raise RetainedEvidenceError("retained_evidence_path_invalid", "retained path is unsafe")
        return Path(*path.parts)

    def _canonical_uuid(self, value: object, field: str) -> str:
        try:
            normalized = str(uuid.UUID(str(value)))
        except (ValueError, TypeError, AttributeError) as error:
            raise RetainedEvidenceError("retained_evidence_identity_invalid", field + " is invalid") from error
        if normalized != value:
            raise RetainedEvidenceError("retained_evidence_identity_invalid", field + " is not canonical")
        return normalized

    def _identity(self, path: Path) -> Dict[str, object]:
        if not path.is_file() or path.is_symlink():
            raise RetainedEvidenceError("retained_file_missing", "retained file is missing")
        digest = hashlib.sha256()
        size = 0
        try:
            with path.open("rb") as source:
                for chunk in iter(lambda: source.read(1024 * 1024), b""):
                    size += len(chunk)
                    digest.update(chunk)
        except OSError as error:
            raise RetainedEvidenceError(
                "retained_file_unreadable", "retained file could not be verified"
            ) from error
        return {"size": size, "sha256": digest.hexdigest()}

    def _load_json(self, path: Path) -> Dict[str, object]:
        if path.is_symlink():
            raise RetainedEvidenceError("retained_json_invalid", "retained JSON path is unsafe")
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, ValueError) as error:
            raise RetainedEvidenceError("retained_json_invalid", "retained JSON is missing or invalid") from error
        if not isinstance(value, dict):
            raise RetainedEvidenceError("retained_json_invalid", "retained JSON must be an object")
        return value

    def _write_json(self, path: Path, value: Mapping[str, object]) -> None:
        with path.open("xb") as destination:
            destination.write(json.dumps(value, sort_keys=True, indent=2, ensure_ascii=False).encode("utf-8") + b"\n")
            destination.flush()
            os.fsync(destination.fileno())

    def _fsync_directory(self, path: Path) -> None:
        descriptor = os.open(str(path), os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def _ensure_directory_tree(self, destination: Path) -> None:
        """Creates evidence ancestors and durably records every new directory entry."""

        try:
            parts = destination.relative_to(self.analysis_root).parts
        except ValueError as error:
            raise RetainedEvidenceError("retained_evidence_path_invalid", "evidence directory escaped its root") from error
        current = self.analysis_root
        if not current.is_dir() or current.is_symlink():
            raise RetainedEvidenceError("retained_evidence_path_invalid", "analysis root is unsafe")
        for part in parts:
            child = current / part
            if child.exists():
                if not child.is_dir() or child.is_symlink():
                    raise RetainedEvidenceError("retained_evidence_path_invalid", "evidence ancestor is unsafe")
            else:
                child.mkdir()
                self._fsync_directory(child)
                self._fsync_directory(current)
            current = child

    def _fsync_publication_chain(self, destination: Path) -> None:
        """Repairs and proves the complete directory-entry chain on every publication attempt."""

        current = destination
        while True:
            if not current.is_dir() or current.is_symlink():
                raise RetainedEvidenceError("retained_evidence_path_invalid", "published evidence path is unsafe")
            self._fsync_directory(current)
            if current == self.analysis_root:
                return
            try:
                current.relative_to(self.analysis_root)
            except ValueError as error:
                raise RetainedEvidenceError("retained_evidence_path_invalid", "published evidence escaped its root") from error
            current = current.parent
