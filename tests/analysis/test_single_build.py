#!/usr/bin/env python3
"""Focused tests for the restricted single-Build LLVM analyzer."""

import hashlib
import json
import sqlite3
import subprocess
import tempfile
import unittest
import uuid
from pathlib import Path
from typing import Dict, List, Optional, Sequence

from ioscoverage.integrations.xcode_artifacts import ArtifactValidationError
from ioscoverage.integrations.git_repository import GitRepositoryError
from ioscoverage.analysis.single_build.analyzer import (
    AnalysisConfiguration,
    SingleBuildAnalysisError,
    SingleBuildAnalyzer,
)
from ioscoverage.storage.retained_evidence import RetainedBuildEvidenceStore


class FakeRepository:
    """Provides controllable read-only Git outcomes to the analyzer."""

    def __init__(self) -> None:
        self.error_code: Optional[str] = None
        self.checkout_validation_calls = 0

    def validate_build_snapshot(self, git_sha: str, target_sha: str, merge_base: str) -> None:
        """Accepts the fixture snapshot unless one stable Git failure is selected."""

        if self.error_code in ("git_object_not_found", "merge_base_mismatch"):
            raise GitRepositoryError(self.error_code, "fixture object is missing")

    def validate_analysis_checkout(self, git_sha: str) -> None:
        """Fails if production accidentally restores a checkout-state dependency."""

        self.checkout_validation_calls += 1
        raise AssertionError("analysis must not inspect HEAD or worktree state")


class FakeArtifactValidator:
    """Provides deterministic UUID validation without requiring a real Mach-O."""

    def __init__(self, controller: "FakeToolController") -> None:
        self.controller = controller

    def validate(
        self,
        artifact_paths: Dict[str, Path],
        declared_uuids: Dict[str, Sequence[str]],
        coverage_binary: str,
        temporary_parent: Path,
        maximum_dsym_uncompressed_bytes: int,
    ) -> Dict[str, List[str]]:
        """Returns declared UUIDs or one selected stable artifact failure."""

        if self.controller.artifact_error:
            raise ArtifactValidationError(
                self.controller.artifact_error, "fixture artifact validation failed"
            )
        return {name: list(values) for name, values in declared_uuids.items()}


class FakeCommandRunner:
    """Creates controlled LLVM outputs while recording analyzer command order."""

    def __init__(self, evidence_dir: Path, controller: "FakeToolController") -> None:
        self.evidence_dir = evidence_dir
        self.controller = controller
        self.records: List[Dict[str, object]] = []
        self.labels: List[str] = []

    def run_for_artifact_validator(
        self, arguments: Sequence[str], timeout_seconds: int
    ) -> subprocess.CompletedProcess:
        """Supports the production validator callback shape if a test needs it."""

        return self.run("artifact-tool", arguments)

    def run(
        self,
        label: str,
        arguments: Sequence[str],
        stdout_filename: Optional[str] = None,
        timeout_seconds: int = 300,
    ) -> subprocess.CompletedProcess:
        """Writes deterministic stdout/stderr and optional generated artifacts."""

        self.labels.append(label)
        stdout = self._stdout(label)
        stderr = "fixture warning\n" if label == self.controller.stderr_label else ""
        returncode = 1 if label == self.controller.failure_label else 0
        if label == "llvm-profdata-merge" and returncode == 0:
            output_index = list(arguments).index("-o") + 1
            Path(arguments[output_index]).write_bytes(b"merged-profile")
        stdout_name = stdout_filename or label + ".stdout.txt"
        stdout_path = self.evidence_dir / stdout_name
        if stdout_filename and label == "llvm-cov-export" and returncode == 0:
            stdout_path.write_text(json.dumps(self.controller.export_payload), encoding="utf-8")
            completed_stdout = ""
        else:
            stdout_path.write_text(stdout, encoding="utf-8")
            completed_stdout = stdout
        stderr_path = self.evidence_dir / (label + ".stderr.txt")
        stderr_path.write_text(stderr, encoding="utf-8")
        self.records.append(
            {
                "label": label,
                "arguments": list(arguments),
                "environment": {"DEVELOPER_DIR": "/Applications/Xcode263.app/Contents/Developer"},
                "started_at": "2026-09-01T00:00:00+00:00",
                "completed_at": "2026-09-01T00:00:01+00:00",
                "exit_code": returncode,
                "timed_out": False,
                "stdout": stdout_name,
                "stderr": stderr_path.name,
                "stdout_size": stdout_path.stat().st_size,
                "stderr_size": stderr_path.stat().st_size,
            }
        )
        return subprocess.CompletedProcess(
            list(arguments), returncode, stdout=completed_stdout, stderr=stderr
        )

    def _stdout(self, label: str) -> str:
        """Returns the minimum valid output for each simulated tool command."""

        if label == "tool-version-xcode":
            return "Xcode 26.3\nBuild version 17C529\n"
        if label in ("tool-version-llvm-profdata", "tool-version-llvm-cov"):
            return "Apple LLVM version 17.0.0\n  Optimized build.\n"
        if label.startswith("llvm-profdata-show"):
            return "Instrumentation level: Front-end\nTotal functions: 2\n"
        return ""


class FakeToolController:
    """Shares failure selection and runner history across analyzer invocations."""

    def __init__(self) -> None:
        self.failure_label: Optional[str] = None
        self.stderr_label: Optional[str] = None
        self.artifact_error: Optional[str] = None
        self.runners: List[FakeCommandRunner] = []
        self.export_payload = {
            "type": "llvm.coverage.json.export",
            "version": "2.0.1",
            "data": [
                {
                    "totals": {
                        "functions": {"count": 2, "covered": 1, "percent": 50.0},
                        "lines": {"count": 4, "covered": 3, "percent": 75.0},
                        "regions": {"count": 5, "covered": 4, "percent": 80.0},
                    },
                    "files": [],
                }
            ],
        }

    def runner_factory(self, evidence_dir: Path, developer_dir: Path) -> FakeCommandRunner:
        """Creates one staged fake runner and retains it for ordering assertions."""

        runner = FakeCommandRunner(evidence_dir, self)
        self.runners.append(runner)
        return runner

    def artifact_factory(self, developer_dir: Path, command_runner: object) -> FakeArtifactValidator:
        """Creates one fake validator using the controller's selected failure."""

        return FakeArtifactValidator(self)


class SingleBuildAnalyzerTests(unittest.TestCase):
    """Validates input boundaries, tool sequencing, publication, and idempotency."""

    def setUp(self) -> None:
        """Creates one isolated SQLite database and immutable Build/Run layout."""

        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.database = self.root / "instance" / "coverage.sqlite3"
        self.data_dir = self.root / "data"
        self.analysis_root = self.root / "analysis" / "single-build"
        self.database.parent.mkdir(parents=True)
        self.cycle_id = str(uuid.uuid4())
        self.build_id = str(uuid.uuid4())
        self.other_cycle_id = str(uuid.uuid4())
        self.other_build_id = str(uuid.uuid4())
        self.run_ids = [str(uuid.uuid4()), str(uuid.uuid4())]
        self.git_sha = "1" * 40
        self.target_sha = "2" * 40
        self.merge_base = "3" * 40
        self.main_bytes = b"fixture-main-macho"
        self.dsym_bytes = b"fixture-dsym-zip"
        self.uuids = [str(uuid.uuid4())]
        self.repository = FakeRepository()
        self.tools = FakeToolController()
        self._create_database()
        self._create_build()
        for index, run_id in enumerate(self.run_ids, start=1):
            self._create_run(run_id, ("profile-" + str(index)).encode("ascii"))
        self.configuration = AnalysisConfiguration(
            database_path=self.database,
            data_dir=self.data_dir,
            analysis_root=self.analysis_root,
            repository_path=self.root / "repository",
        )

    def tearDown(self) -> None:
        """Removes all isolated files after every test."""

        self.temporary.cleanup()

    def analyzer(self) -> SingleBuildAnalyzer:
        """Creates the analyzer with controllable Git, artifact, and subprocess dependencies."""

        return SingleBuildAnalyzer(
            self.configuration,
            repository=self.repository,
            command_runner_factory=self.tools.runner_factory,
            artifact_validator_factory=self.tools.artifact_factory,
        )

    def test_valid_multi_run_analysis_publishes_complete_result_without_database_change(self) -> None:
        """Publishes every required evidence file and leaves SQLite byte-identical."""

        database_sha = self._sha256(self.database)
        result = self.analyzer().analyze(self.cycle_id, self.build_id, self.run_ids)
        self.assertFalse(result.idempotent_replay)
        self.assertEqual(result.summary["profraw_count"], 2)
        self.assertEqual(result.summary["coverage"]["lines"]["covered"], 3)
        for filename in (
            "inputs.json",
            "tool_versions.json",
            "commands.json",
            "merged.profdata",
            "llvm-cov-export.json",
            "summary.json",
            "completion.json",
        ):
            self.assertTrue((result.output_directory / filename).is_file(), filename)
        self.assertEqual(self._sha256(self.database), database_sha)

    def test_duplicate_run_id_is_rejected(self) -> None:
        """Rejects duplicate identities before any LLVM process starts."""

        with self.assertRaisesRegex(SingleBuildAnalysisError, "unique"):
            self.analyzer().analyze(
                self.cycle_id, self.build_id, [self.run_ids[0], self.run_ids[0]]
            )
        self.assertFalse(self.tools.runners)

    def test_run_from_another_cycle_or_build_is_rejected(self) -> None:
        """Rejects any Run whose persisted Cycle or Build differs from the request."""

        for column, value, code in (
            ("cycle_id", self.other_cycle_id, "run_cycle_mismatch"),
            ("build_id", self.other_build_id, "run_build_mismatch"),
        ):
            with self.subTest(column=column):
                self._update_run(self.run_ids[0], column, value)
                with self.assertRaises(SingleBuildAnalysisError) as context:
                    self.analyzer().analyze(self.cycle_id, self.build_id, [self.run_ids[0]])
                self.assertEqual(context.exception.code, code)
                original = self.cycle_id if column == "cycle_id" else self.build_id
                self._update_run(self.run_ids[0], column, original)

    def test_run_state_analyzable_and_error_are_enforced(self) -> None:
        """Requires uploaded, analyzable, error-free Run metadata."""

        cases = (
            ("state", "pending_build", "run_not_analyzable"),
            ("analyzable", 0, "run_not_analyzable"),
            ("error_message", "failure", "run_has_error"),
        )
        for column, value, code in cases:
            with self.subTest(column=column):
                original = {"state": "uploaded", "analyzable": 1, "error_message": None}[column]
                self._update_run(self.run_ids[0], column, value)
                with self.assertRaises(SingleBuildAnalysisError) as context:
                    self.analyzer().analyze(self.cycle_id, self.build_id, [self.run_ids[0]])
                self.assertEqual(context.exception.code, code)
                self._update_run(self.run_ids[0], column, original)

    def test_profraw_missing_size_and_sha_mismatch_are_rejected(self) -> None:
        """Rejects missing or changed profile bytes before LLVM starts."""

        profile = self._run_directory(self.run_ids[0]) / "payload.profraw"
        original = profile.read_bytes()
        for mutation, code in (
            (b"PROFILE-1", "profraw_sha256_mismatch"),
            (b"short", "profraw_size_mismatch"),
        ):
            with self.subTest(code=code):
                profile.write_bytes(mutation)
                with self.assertRaises(SingleBuildAnalysisError) as context:
                    self.analyzer().analyze(self.cycle_id, self.build_id, [self.run_ids[0]])
                self.assertEqual(context.exception.code, code)
                profile.write_bytes(original)
        profile.unlink()
        with self.assertRaises(SingleBuildAnalysisError) as context:
            self.analyzer().analyze(self.cycle_id, self.build_id, [self.run_ids[0]])
        self.assertEqual(context.exception.code, "stored_file_missing")

    def test_build_artifact_missing_size_sha_and_uuid_mismatch_are_rejected(self) -> None:
        """Rejects every form of Build artifact integrity failure before LLVM."""

        main = self._build_directory() / "artifacts" / "main_executable.bin"
        original = main.read_bytes()
        main.write_bytes(b"short")
        with self.assertRaises(SingleBuildAnalysisError) as context:
            self.analyzer().analyze(self.cycle_id, self.build_id, [self.run_ids[0]])
        self.assertEqual(context.exception.code, "artifact_size_mismatch")
        main.write_bytes(b"X" * len(original))
        with self.assertRaises(SingleBuildAnalysisError) as context:
            self.analyzer().analyze(self.cycle_id, self.build_id, [self.run_ids[0]])
        self.assertEqual(context.exception.code, "artifact_sha256_mismatch")
        main.write_bytes(original)
        self.tools.artifact_error = "artifact_uuid_mismatch"
        with self.assertRaises(SingleBuildAnalysisError) as context:
            self.analyzer().analyze(self.cycle_id, self.build_id, [self.run_ids[0]])
        self.assertEqual(context.exception.code, "artifact_uuid_mismatch")
        self.tools.artifact_error = None
        main.unlink()
        with self.assertRaises(SingleBuildAnalysisError) as context:
            self.analyzer().analyze(self.cycle_id, self.build_id, [self.run_ids[0]])
        self.assertEqual(context.exception.code, "stored_file_missing")

    def test_invalid_or_missing_coverage_binary_is_rejected(self) -> None:
        """Never guesses or switches away from the manifest-selected analysis object."""

        self._update_build_manifest("copied_placeholder")
        with self.assertRaises(SingleBuildAnalysisError) as context:
            self.analyzer().analyze(self.cycle_id, self.build_id, [self.run_ids[0]])
        self.assertEqual(context.exception.code, "invalid_coverage_binary")
        self._update_build_manifest("debug_dylib")
        with self.assertRaises(SingleBuildAnalysisError) as context:
            self.analyzer().analyze(self.cycle_id, self.build_id, [self.run_ids[0]])
        self.assertEqual(context.exception.code, "coverage_binary_missing")

    def test_git_checkout_state_is_not_consulted(self) -> None:
        """Analyzes immutable objects without asking for HEAD or worktree state."""

        result = self.analyzer().analyze(self.cycle_id, self.build_id, [self.run_ids[0]])
        self.assertEqual(result.summary["git_sha"], self.git_sha)
        self.assertEqual(self.repository.checkout_validation_calls, 0)

    def test_missing_git_object_and_wrong_merge_base_are_rejected(self) -> None:
        """Retains immutable object and merge-base validation after removing checkout checks."""

        for code in ("git_object_not_found", "merge_base_mismatch"):
            with self.subTest(code=code):
                self.repository.error_code = code
                with self.assertRaises(SingleBuildAnalysisError) as context:
                    self.analyzer().analyze(self.cycle_id, self.build_id, [self.run_ids[0]])
                self.assertEqual(context.exception.code, code)

    def test_raw_profile_show_failure_stops_before_merge(self) -> None:
        """Never merges after one independently unreadable raw profile."""

        self.tools.failure_label = "llvm-profdata-show-run-01"
        with self.assertRaises(SingleBuildAnalysisError):
            self.analyzer().analyze(self.cycle_id, self.build_id, self.run_ids)
        labels = self.tools.runners[-1].labels
        self.assertNotIn("llvm-profdata-merge", labels)
        self.assertNotIn("llvm-cov-export", labels)

    def test_merge_failure_stops_before_llvm_cov(self) -> None:
        """Never invokes llvm-cov after llvm-profdata merge fails."""

        self.tools.failure_label = "llvm-profdata-merge"
        with self.assertRaises(SingleBuildAnalysisError):
            self.analyzer().analyze(self.cycle_id, self.build_id, self.run_ids)
        self.assertNotIn("llvm-cov-export", self.tools.runners[-1].labels)

    def test_llvm_cov_nonzero_or_stderr_is_fatal(self) -> None:
        """Applies the no-warning-whitelist policy to llvm-cov export."""

        self.tools.failure_label = "llvm-cov-export"
        with self.assertRaises(SingleBuildAnalysisError) as context:
            self.analyzer().analyze(self.cycle_id, self.build_id, self.run_ids)
        self.assertEqual(context.exception.code, "llvm_command_failed")
        self.tools.failure_label = None
        self.tools.stderr_label = "llvm-cov-export"
        with self.assertRaises(SingleBuildAnalysisError) as context:
            self.analyzer().analyze(self.cycle_id, self.build_id, self.run_ids)
        self.assertEqual(context.exception.code, "llvm_stderr_not_empty")

    def test_failed_staging_never_publishes_a_complete_result(self) -> None:
        """Preserves explicit failure evidence without creating a final hash directory."""

        self.tools.failure_label = "llvm-profdata-merge"
        with self.assertRaises(SingleBuildAnalysisError):
            self.analyzer().analyze(self.cycle_id, self.build_id, self.run_ids)
        published = list((self.analysis_root / self.build_id).glob("[0-9a-f]" * 64))
        self.assertEqual(published, [])
        failures = list((self.analysis_root / ".failed" / self.build_id).glob("*/failure.json"))
        self.assertTrue(failures)
        self.assertEqual(json.loads(failures[-1].read_text())["status"], "failed")

    def test_identical_input_is_an_idempotent_replay(self) -> None:
        """Returns the verified existing bytes without rerunning LLVM commands."""

        first = self.analyzer().analyze(self.cycle_id, self.build_id, self.run_ids)
        second = self.analyzer().analyze(self.cycle_id, self.build_id, list(reversed(self.run_ids)))
        self.assertFalse(first.idempotent_replay)
        self.assertTrue(second.idempotent_replay)
        self.assertEqual(first.input_set_sha256, second.input_set_sha256)
        self.assertEqual(first.summary, second.summary)
        self.assertEqual(self.tools.runners[-1].labels, [])

    def test_different_run_sets_have_different_input_hashes(self) -> None:
        """Makes the exact selected Run identity part of the output namespace."""

        first = self.analyzer().analyze(self.cycle_id, self.build_id, [self.run_ids[0]])
        second = self.analyzer().analyze(self.cycle_id, self.build_id, self.run_ids)
        self.assertNotEqual(first.input_set_sha256, second.input_set_sha256)
        self.assertNotEqual(first.output_directory, second.output_directory)

    def test_retained_same_build_baseline_merges_only_new_raw_profile(self) -> None:
        """Keeps old hits after profraw/dSYM cleanup without cross-Build profile merging."""

        first = self.analyzer().analyze(self.cycle_id, self.build_id, self.run_ids)
        evidence = RetainedBuildEvidenceStore(self.analysis_root.parent)
        evidence.publish(self.cycle_id, self.build_id, first.output_directory, ["dsym"])
        for run_id in self.run_ids:
            (self._run_directory(run_id) / "payload.profraw").unlink()
        (self._build_directory() / "artifacts" / "dsym.zip").unlink()
        new_run = str(uuid.uuid4())
        self._create_run(new_run, b"profile-new")
        result = self.analyzer().analyze(
            self.cycle_id, self.build_id, self.run_ids + [new_run]
        )
        labels = self.tools.runners[-1].labels
        self.assertIn("llvm-profdata-show-retained-baseline", labels)
        self.assertEqual(sum(label.startswith("llvm-profdata-show-run") for label in labels), 1)
        merge = next(record for record in self.tools.runners[-1].records if record["label"] == "llvm-profdata-merge")
        self.assertIn(str(first.output_directory / "merged.profdata"), merge["arguments"])
        self.assertIn(str(self._run_directory(new_run) / "payload.profraw"), merge["arguments"])
        self.assertEqual(result.summary["profraw_count"], 3)

    def _create_database(self) -> None:
        """Creates only the production columns read by the analyzer."""

        with sqlite3.connect(str(self.database)) as connection:
            connection.executescript(
                """
                CREATE TABLE coverage_cycles (id TEXT PRIMARY KEY);
                CREATE TABLE builds (
                    id TEXT PRIMARY KEY, cycle_id TEXT, git_sha TEXT,
                    target_sha_at_build TEXT, merge_base_sha TEXT,
                    coverage_binary TEXT, artifact_state TEXT,
                    manifest_sha256 TEXT, manifest_json TEXT,
                    main_executable_sha256 TEXT, main_executable_size INTEGER,
                    debug_dylib_sha256 TEXT, debug_dylib_size INTEGER,
                    dsym_sha256 TEXT, dsym_size INTEGER,
                    main_executable_uuids_json TEXT,
                    debug_dylib_uuids_json TEXT, dsym_uuids_json TEXT
                );
                CREATE TABLE runs (
                    id TEXT PRIMARY KEY, cycle_id TEXT, build_id TEXT,
                    state TEXT, analyzable INTEGER, error_message TEXT,
                    metadata_sha256 TEXT, metadata_json TEXT,
                    payload_sha256 TEXT, payload_size INTEGER
                );
                """
            )
            connection.execute("INSERT INTO coverage_cycles (id) VALUES (?)", (self.cycle_id,))
            connection.execute("INSERT INTO coverage_cycles (id) VALUES (?)", (self.other_cycle_id,))

    def _create_build(self) -> None:
        """Persists one valid main-executable fixture Build."""

        main_sha = hashlib.sha256(self.main_bytes).hexdigest()
        dsym_sha = hashlib.sha256(self.dsym_bytes).hexdigest()
        manifest = {
            "schema_version": 1,
            "build_id": self.build_id,
            "cycle_id": self.cycle_id,
            "git_sha": self.git_sha,
            "target_sha_at_build": self.target_sha,
            "merge_base_sha": self.merge_base,
            "coverage_binary": "main_executable",
            "bundle_id": "com.example.coverage",
            "app_version": "1.0",
            "bundle_version": "1",
            "xcode_version": "26.3",
            "swift_version": "6.2",
            "macho_uuids": {"main_executable": self.uuids, "dsym": self.uuids},
            "artifacts": {
                "main_executable": {
                    "filename": "ExampleApp",
                    "size": len(self.main_bytes),
                    "sha256": main_sha,
                },
                "dsym": {
                    "filename": "ExampleApp.app.dSYM.zip",
                    "archive_format": "zip",
                    "size": len(self.dsym_bytes),
                    "sha256": dsym_sha,
                },
            },
        }
        canonical = self._canonical(manifest)
        build_directory = self._build_directory()
        artifact_directory = build_directory / "artifacts"
        artifact_directory.mkdir(parents=True)
        (build_directory / "manifest.json").write_bytes(canonical + b"\n")
        (artifact_directory / "main_executable.bin").write_bytes(self.main_bytes)
        (artifact_directory / "dsym.zip").write_bytes(self.dsym_bytes)
        with sqlite3.connect(str(self.database)) as connection:
            connection.execute(
                """
                INSERT INTO builds VALUES (
                    ?, ?, ?, ?, ?, 'main_executable', 'ready', ?, ?,
                    ?, ?, NULL, NULL, ?, ?, ?, NULL, ?
                )
                """,
                (
                    self.build_id,
                    self.cycle_id,
                    self.git_sha,
                    self.target_sha,
                    self.merge_base,
                    hashlib.sha256(canonical).hexdigest(),
                    canonical.decode("utf-8"),
                    main_sha,
                    len(self.main_bytes),
                    dsym_sha,
                    len(self.dsym_bytes),
                    json.dumps(self.uuids),
                    json.dumps(self.uuids),
                ),
            )

    def _create_run(self, run_id: str, profile: bytes) -> None:
        """Persists one valid uploaded and analyzable Run fixture."""

        metadata = {
            "schema_version": 1,
            "run_id": run_id,
            "cycle_id": self.cycle_id,
            "build_id": self.build_id,
            "device_id": str(uuid.uuid4()),
            "process_id": 42,
            "launch_id": str(uuid.uuid4()),
            "captured_at": "2026-09-01T00:00:00Z",
        }
        canonical = self._canonical(metadata)
        payload_sha = hashlib.sha256(profile).hexdigest()
        directory = self._run_directory(run_id)
        directory.mkdir(parents=True)
        (directory / "metadata.json").write_bytes(canonical + b"\n")
        (directory / "payload.profraw").write_bytes(profile)
        (directory / "upload.sha256").write_text(payload_sha + "\n", encoding="ascii")
        with sqlite3.connect(str(self.database)) as connection:
            connection.execute(
                "INSERT INTO runs VALUES (?, ?, ?, 'uploaded', 1, NULL, ?, ?, ?, ?)",
                (
                    run_id,
                    self.cycle_id,
                    self.build_id,
                    hashlib.sha256(canonical).hexdigest(),
                    canonical.decode("utf-8"),
                    payload_sha,
                    len(profile),
                ),
            )

    def _update_run(self, run_id: str, column: str, value: object) -> None:
        """Updates one controlled fixture column using a fixed allowlist."""

        if column not in {"cycle_id", "build_id", "state", "analyzable", "error_message"}:
            raise ValueError("unsupported fixture column")
        with sqlite3.connect(str(self.database)) as connection:
            connection.execute("UPDATE runs SET " + column + " = ? WHERE id = ?", (value, run_id))

    def _update_build_manifest(self, coverage_binary: str) -> None:
        """Keeps SQLite and manifest bytes consistent while changing the selected subject."""

        build_directory = self._build_directory()
        manifest = json.loads((build_directory / "manifest.json").read_text())
        manifest["coverage_binary"] = coverage_binary
        canonical = self._canonical(manifest)
        (build_directory / "manifest.json").write_bytes(canonical + b"\n")
        with sqlite3.connect(str(self.database)) as connection:
            connection.execute(
                "UPDATE builds SET coverage_binary = ?, manifest_json = ?, manifest_sha256 = ? WHERE id = ?",
                (
                    coverage_binary,
                    canonical.decode("utf-8"),
                    hashlib.sha256(canonical).hexdigest(),
                    self.build_id,
                ),
            )

    def _build_directory(self) -> Path:
        """Returns the fixture Build directory."""

        return self.data_dir / "cycles" / self.cycle_id / "builds" / self.build_id

    def _run_directory(self, run_id: str) -> Path:
        """Returns one fixture Run directory."""

        return self.data_dir / "cycles" / self.cycle_id / "runs" / run_id

    def _canonical(self, value: object) -> bytes:
        """Matches production semantic JSON hashing."""

        return json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")

    def _sha256(self, path: Path) -> str:
        """Hashes one fixture file for mutation checks."""

        return hashlib.sha256(path.read_bytes()).hexdigest()


if __name__ == "__main__":
    unittest.main()
