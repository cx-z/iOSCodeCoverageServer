"""Tests for dependency-driven runtime cleanup and retained analysis evidence."""

import hashlib
import json
import shutil
import sqlite3
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

from ioscoverage.lifecycle.cleanup import RuntimeCleanupError, RuntimeDataCleaner
from ioscoverage.lifecycle.runtime_lock import RuntimeLockError, RuntimeProcessLock
from ioscoverage.lifecycle.secure_delete import SecureDeletionError, SecureRuntimeDeletion
from ioscoverage.runtime import RuntimePaths
from ioscoverage.storage.analysis_state import AnalysisStateStore
from ioscoverage.storage.reports import CoverageReportStore
from ioscoverage.storage.retained_evidence import RetainedBuildEvidenceStore, RetainedEvidenceError
from ioscoverage.cli.cleanup_runtime_data import build_parser


class CleanupFixture:
    """Creates one complete report graph plus redundant runtime inputs."""

    def __init__(self, coverage_binary="main_executable"):
        self.temporary = tempfile.TemporaryDirectory(prefix="ios-coverage-cleanup-tests-")
        self.paths = RuntimePaths.from_root(Path(self.temporary.name))
        self.paths.prepare_directories()
        self.cycle_id = str(uuid.uuid4())
        self.build_id = str(uuid.uuid4())
        self.run_id = str(uuid.uuid4())
        self.input_sha = "1" * 64
        self.diff_sha = "2" * 64
        self.cycle_sha = "3" * 64
        self.orchestration_sha = "4" * 64
        self.coverage_binary = coverage_binary
        self._data()
        self._database()
        self._analysis()
        self.state_store = AnalysisStateStore(self.paths.database)
        self.state_store.request_analysis(self.cycle_id, "manual_retry")
        self.state_store.claim(self.cycle_id)
        self.state_store.complete_success(self.cycle_id, 1, {
            "orchestration_input_sha256": self.orchestration_sha,
            "cycle_input_set_sha256": self.cycle_sha,
            "current_build_id": self.build_id,
            "output_directory": str(self.orchestration_dir),
            "summary": {"covered": 1},
        })

    def close(self):
        self.temporary.cleanup()

    def _database(self):
        self.paths.database.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(str(self.paths.database)) as connection:
            connection.executescript("""
                CREATE TABLE coverage_cycles (id TEXT PRIMARY KEY, project TEXT);
                CREATE TABLE builds (
                  id TEXT PRIMARY KEY, cycle_id TEXT, coverage_binary TEXT,
                  artifact_state TEXT, manifest_sha256 TEXT, manifest_json TEXT);
                CREATE TABLE runs (
                  id TEXT PRIMARY KEY, cycle_id TEXT, build_id TEXT,
                  metadata_sha256 TEXT, payload_sha256 TEXT, payload_size INTEGER,
                  state TEXT, analyzable INTEGER, error_message TEXT);
                PRAGMA user_version = 2;
            """)
            connection.execute("INSERT INTO coverage_cycles VALUES (?, 'ExampleApp')", (self.cycle_id,))
            connection.execute(
                "INSERT INTO builds VALUES (?, ?, ?, 'ready', ?, ?)",
                (
                    self.build_id,
                    self.cycle_id,
                    self.coverage_binary,
                    self.manifest_sha,
                    self._canonical_json(self.manifest).decode("utf-8"),
                ),
            )
            connection.execute(
                "INSERT INTO runs VALUES (?, ?, ?, ?, ?, ?, 'uploaded', 1, NULL)",
                (
                    self.run_id,
                    self.cycle_id,
                    self.build_id,
                    self.metadata_sha,
                    self.payload_sha,
                    len(self.payload),
                ),
            )

    def _data(self):
        self.build_dir = self.paths.data_dir / "cycles" / self.cycle_id / "builds" / self.build_id
        artifacts = self.build_dir / "artifacts"
        artifacts.mkdir(parents=True)
        self.main = b"main-binary"
        self.dsym = b"debug-symbol"
        self.debug_dylib = b"debug-library"
        self.manifest = {
            "artifacts": {
                "main_executable": {
                    "size": len(self.main), "sha256": self._sha256(self.main),
                    "filename": "ExampleApp",
                },
                "dsym": {
                    "size": len(self.dsym), "sha256": self._sha256(self.dsym),
                    "filename": "ExampleApp.dSYM.zip",
                },
            }
        }
        if self.coverage_binary == "debug_dylib":
            self.manifest["artifacts"]["debug_dylib"] = {
                "size": len(self.debug_dylib), "sha256": self._sha256(self.debug_dylib),
                "filename": "ExampleApp.debug.dylib",
            }
        self.manifest_sha = hashlib.sha256(self._canonical_json(self.manifest)).hexdigest()
        self._write_json(self.build_dir / "manifest.json", self.manifest)
        (artifacts / "main_executable.bin").write_bytes(self.main)
        (artifacts / "dsym.zip").write_bytes(self.dsym)
        if self.coverage_binary == "debug_dylib":
            (artifacts / "debug_dylib.bin").write_bytes(self.debug_dylib)
        self.run_dir = self.paths.data_dir / "cycles" / self.cycle_id / "runs" / self.run_id
        self.run_dir.mkdir(parents=True)
        self.metadata = {"run_id": self.run_id}
        self.metadata_sha = hashlib.sha256(self._canonical_json(self.metadata)).hexdigest()
        self.payload = b"profile-payload"
        self.payload_sha = self._sha256(self.payload)
        self._write_json(self.run_dir / "metadata.json", self.metadata)
        (self.run_dir / "payload.profraw").write_bytes(self.payload)
        (self.run_dir / "upload.sha256").write_text(self.payload_sha + "\n")

    def _analysis(self):
        self.single_dir = self.paths.analysis_dir / "single-build" / self.build_id / self.input_sha
        self.single_dir.mkdir(parents=True)
        inputs = {
            "schema_version": 1,
            "cycle_id": self.cycle_id, "build_id": self.build_id,
            "input_set_sha256": self.input_sha,
            "build": {
                "manifest_sha256": self.manifest_sha, "coverage_binary": self.coverage_binary,
                "actual_macho_uuids": {
                    name: ["A"] for name in self.manifest["artifacts"]
                },
                "manifest": self.manifest,
            },
            "runs": [{
                "run_id": self.run_id, "payload_size": len(self.payload),
                "payload_sha256": self.payload_sha,
                "metadata_sha256": self.metadata_sha,
            }],
        }
        self._write_json(self.single_dir / "inputs.json", inputs)
        (self.single_dir / "merged.profdata").write_bytes(b"merged")
        self._write_json(self.single_dir / "llvm-cov-export.json", {"type": "llvm.coverage.json.export"})
        self._write_json(self.single_dir / "summary.json", {
            "schema_version": 1, "status": "complete", "cycle_id": self.cycle_id,
            "build_id": self.build_id, "input_set_sha256": self.input_sha,
        })
        self._complete(self.single_dir, "completion.json", "input_set_sha256", self.input_sha)

        self.diff_dir = self.paths.analysis_dir / "single-build-diff" / self.build_id / self.input_sha / self.diff_sha
        self.diff_dir.mkdir(parents=True)
        diff_identity = {
            "schema_version": 1, "cycle_id": self.cycle_id, "build_id": self.build_id,
            "input_set_sha256": self.input_sha, "diff_set_sha256": self.diff_sha,
        }
        self._write_json(self.diff_dir / "inputs.json", diff_identity)
        self._write_json(self.diff_dir / "summary.json", {**diff_identity, "status": "complete"})
        self._complete(self.diff_dir, "completion.json", "diff_set_sha256", self.diff_sha)

        self.cycle_dir = self.paths.analysis_dir / "cycle-coverage" / self.cycle_id / self.build_id / self.cycle_sha
        self.cycle_dir.mkdir(parents=True)
        summary = {
            "schema_version": 1, "status": "complete", "cycle_id": self.cycle_id,
            "current_build_id": self.build_id, "cycle_input_set_sha256": self.cycle_sha,
            "changed_line_coverage_percent": 100.0,
            "lines": {"changed_new_side": 1, "measurable": 1, "covered": 1,
                      "uncovered": 0, "not_measurable": 0, "unknown": 0,
                      "deleted_excluded": 0, "covered_with_inherited_evidence": 0},
            "per_file": [{"repo_relative_path": "Sources/Feature.swift", "git_status": "M",
                          "changed_new_side": 1, "measurable": 1, "covered": 1,
                          "uncovered": 0, "not_measurable": 0, "unknown": 0,
                          "deleted_excluded": 0}],
        }
        self._write_json(self.cycle_dir / "manifest.json", {
            "schema_version": 1, "cycle_id": self.cycle_id,
            "current_build": {"build_id": self.build_id},
            "cycle_input_set_sha256": self.cycle_sha,
        })
        self._write_json(self.cycle_dir / "summary.json", summary)
        self._write_json(self.cycle_dir / "cumulative-line-coverage.json", {"lines": [{
            "repo_relative_path": "Sources/Feature.swift", "new_line_number": 1,
            "source_text": "return true", "source_text_sha256": "a" * 64,
            "state": "covered", "reason": "fixture", "regions": [],
        }]})
        self._complete(self.cycle_dir, "complete.json", "cycle_input_set_sha256", self.cycle_sha)

        self.orchestration_dir = self.paths.analysis_dir / "cycle-latest" / self.cycle_id / self.orchestration_sha
        self.orchestration_dir.mkdir(parents=True)
        results = {
            "schema_version": 1,
            "single_build_results": [self._reference(self.single_dir, "completion.json", {
                "build_id": self.build_id, "input_set_sha256": self.input_sha})],
            "current_diff_result": self._reference(self.diff_dir, "completion.json", {"diff_set_sha256": self.diff_sha}),
            "cycle_coverage_result": self._reference(self.cycle_dir, "complete.json", {"cycle_input_set_sha256": self.cycle_sha}),
        }
        self._write_json(self.orchestration_dir / "manifest.json", {
            "schema_version": 1, "cycle": {"cycle_id": self.cycle_id},
            "current_build_id": self.build_id,
        })
        self._write_json(self.orchestration_dir / "results.json", results)
        self._write_json(self.orchestration_dir / "summary.json", {
            "schema_version": 1, "status": "complete", "cycle_id": self.cycle_id,
            "current_build_id": self.build_id,
            "orchestration_input_sha256": self.orchestration_sha,
            "cycle_input_set_sha256": self.cycle_sha,
        })
        self._complete(self.orchestration_dir, "complete.json", "orchestration_input_sha256", self.orchestration_sha)

        self.old_dir = self.paths.analysis_dir / "cycle-latest" / self.cycle_id / ("b" * 64)
        self.old_dir.mkdir(parents=True)
        self._write_json(self.old_dir / "manifest.json", {
            "schema_version": 1, "cycle": {"cycle_id": self.cycle_id},
            "current_build_id": self.build_id,
        })
        self._write_json(self.old_dir / "summary.json", {
            "schema_version": 1, "status": "complete", "cycle_id": self.cycle_id,
            "current_build_id": self.build_id,
            "orchestration_input_sha256": "b" * 64,
        })
        self._write_json(self.old_dir / "results.json", results)
        self._complete(self.old_dir, "complete.json", "orchestration_input_sha256", "b" * 64)

    def _reference(self, directory, completion_name, identity):
        return {**identity, "output_directory": str(directory.resolve()),
                "completion": self._identity(directory / completion_name),
                "summary": self._identity(directory / "summary.json")}

    def _complete(self, directory, name, key, value):
        outputs = {p.relative_to(directory).as_posix(): self._identity(p)
                   for p in directory.rglob("*") if p.is_file() and p.name != name}
        self._write_json(directory / name, {
            "schema_version": 1, "status": "complete", key: value, "outputs": outputs,
        })

    def _identity(self, path):
        data = path.read_bytes()
        return {"size": len(data), "sha256": hashlib.sha256(data).hexdigest()}

    def _write_json(self, path, value):
        path.write_text(json.dumps(value, sort_keys=True) + "\n", encoding="utf-8")

    def _canonical_json(self, value):
        return json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")

    def _sha256(self, value):
        return hashlib.sha256(value).hexdigest()

    def tree_identity(self):
        return {p.relative_to(self.paths.root).as_posix(): self._identity(p)
                for p in self.paths.root.rglob("*") if p.is_file() and not p.is_symlink()}


class RuntimeDataCleanerTests(unittest.TestCase):
    """Protects latest reports while compacting only proven redundant inputs."""

    def setUp(self):
        self.fixture = CleanupFixture()

    def tearDown(self):
        self.fixture.close()

    def test_dry_run_is_byte_read_only_and_reports_categories(self):
        before = self.fixture.tree_identity()
        result = RuntimeDataCleaner(self.fixture.paths).run()
        self.assertTrue(result["dry_run"])
        self.assertEqual(before, self.fixture.tree_identity())
        self.assertEqual(result["scanned"]["runs"], 1)
        self.assertGreater(result["planned_release_bytes"], 0)

    def test_apply_keeps_latest_report_and_baseline_but_deletes_redundancy(self):
        result = RuntimeDataCleaner(self.fixture.paths).run(apply=True)
        self.assertFalse(result["dry_run"])
        self.assertFalse((self.fixture.run_dir / "payload.profraw").exists())
        self.assertFalse((self.fixture.build_dir / "artifacts" / "dsym.zip").exists())
        self.assertTrue((self.fixture.build_dir / "artifacts" / "main_executable.bin").is_file())
        self.assertTrue((self.fixture.single_dir / "merged.profdata").is_file())
        self.assertFalse(self.fixture.old_dir.exists())
        report = CoverageReportStore(
            self.fixture.paths.analysis_dir, AnalysisStateStore(self.fixture.paths.database)
        ).get_report(self.fixture.cycle_id)
        self.assertEqual(report["summary"]["covered"], 1)

    def test_apply_is_idempotent_and_preserves_database_tokens_and_registry(self):
        self.fixture.paths.projects_file.write_text("{}\n")
        self.fixture.paths.operator_token.write_text("fixture-not-a-real-token\n")
        protected = {p: p.read_bytes() for p in (
            self.fixture.paths.database, self.fixture.paths.projects_file, self.fixture.paths.operator_token
        )}
        RuntimeDataCleaner(self.fixture.paths).run(apply=True)
        second = RuntimeDataCleaner(self.fixture.paths).run(apply=True)
        self.assertEqual(second["released_bytes"], 0)
        self.assertEqual(protected, {p: p.read_bytes() for p in protected})

    def test_active_analysis_and_runtime_lock_block_apply(self):
        self.fixture.state_store.request_analysis(self.fixture.cycle_id, "manual_retry")
        with self.assertRaises(RuntimeCleanupError) as active:
            RuntimeDataCleaner(self.fixture.paths).run(apply=True)
        self.assertEqual(active.exception.code, "analysis_active")
        with sqlite3.connect(str(self.fixture.paths.database)) as connection:
            connection.execute("UPDATE cycle_analysis_states SET status='succeeded'")
        lock = RuntimeProcessLock(self.fixture.paths.server_lock)
        lock.acquire()
        try:
            with self.assertRaises(RuntimeCleanupError) as running:
                RuntimeDataCleaner(self.fixture.paths).run(apply=True)
            self.assertEqual(running.exception.code, "service_running")
        finally:
            lock.release()

    def test_apply_holds_runtime_lock_before_snapshot_and_planning(self):
        """Prevents a server from publishing a newer latest success during planning."""

        fixture = self.fixture
        test_case = self

        class LockObservingCleaner(RuntimeDataCleaner):
            def _snapshot(self):
                contender = RuntimeProcessLock(fixture.paths.server_lock)
                with test_case.assertRaises(RuntimeLockError):
                    contender.acquire()
                return super()._snapshot()

        LockObservingCleaner(self.fixture.paths).run(apply=True)

    def test_fsync_failure_is_bounded_and_retry_repairs_publication_durability(self):
        """Never deletes raw input until an existing evidence path is fsynced on retry."""

        cleaner = RuntimeDataCleaner(self.fixture.paths)
        destination = cleaner.evidence_store.evidence_directory(
            self.fixture.cycle_id, self.fixture.build_id, self.fixture.input_sha
        )
        original = cleaner.evidence_store._fsync_directory
        failed = {"value": False}

        def fail_parent_once(path):
            if path == destination.parent and destination.exists() and not failed["value"]:
                failed["value"] = True
                raise OSError("injected fsync failure")
            original(path)

        cleaner.evidence_store._fsync_directory = fail_parent_once
        with self.assertRaises(RuntimeCleanupError) as caught:
            cleaner.run(apply=True)
        self.assertEqual(caught.exception.code, "cleanup_io_failed")
        self.assertTrue((self.fixture.run_dir / "payload.profraw").is_file())

        retry = RuntimeDataCleaner(self.fixture.paths)
        retry_original = retry.evidence_store._fsync_directory
        synced = []

        def record_sync(path):
            synced.append(path)
            retry_original(path)

        retry.evidence_store._fsync_directory = record_sync
        retry.run(apply=True)
        self.assertIn(destination.parent, synced)
        self.assertIn(self.fixture.paths.analysis_dir, synced)
        self.assertFalse((self.fixture.run_dir / "payload.profraw").exists())

    def test_publication_failure_is_reported_as_bounded_cleanup_error(self):
        """Prevents evidence or filesystem exceptions from exposing raw terminal tracebacks."""

        cleaner = RuntimeDataCleaner(self.fixture.paths)

        def fail_publish(*_arguments):
            raise RetainedEvidenceError("fixture_internal", "unsafe local detail")

        cleaner.evidence_store.publish = fail_publish
        with self.assertRaises(RuntimeCleanupError) as caught:
            cleaner.run(apply=True)
        self.assertEqual(caught.exception.code, "retained_evidence_failed")
        self.assertEqual(
            caught.exception.message, "retained Build evidence could not be published"
        )
        self.assertTrue((self.fixture.run_dir / "payload.profraw").is_file())

    def test_tampered_profraw_is_not_a_deletion_candidate(self):
        """Retains a Run path whose current bytes no longer match the successful input."""

        profile = self.fixture.run_dir / "payload.profraw"
        profile.write_bytes(b"X" * len(self.fixture.payload))
        result = RuntimeDataCleaner(self.fixture.paths).run(apply=True)
        self.assertTrue(profile.is_file())
        self.assertFalse(result["planned_deletion"])
        self.assertEqual(
            result["skipped"][0]["reason"], "cleanup_candidate_identity_mismatch"
        )

    def test_tampered_artifact_is_not_a_deletion_candidate(self):
        """Retains a non-coverage artifact whose current SHA differs from its manifest."""

        dsym = self.fixture.build_dir / "artifacts" / "dsym.zip"
        dsym.write_bytes(b"X" * len(self.fixture.dsym))
        result = RuntimeDataCleaner(self.fixture.paths).run(apply=True)
        self.assertTrue(dsym.is_file())
        self.assertTrue((self.fixture.run_dir / "payload.profraw").is_file())
        self.assertFalse(result["planned_deletion"])
        self.assertEqual(
            result["skipped"][0]["reason"], "cleanup_candidate_identity_mismatch"
        )

    def test_missing_or_drifted_sqlite_run_blocks_cycle_cleanup(self):
        """Requires every incorporated Run to retain its exact current SQLite identity."""

        mutations = (
            "DELETE FROM runs WHERE id = ?",
            "UPDATE runs SET payload_sha256 = ? WHERE id = ?",
        )
        for index, statement in enumerate(mutations):
            with self.subTest(mutation=index):
                fixture = self.fixture if index == 0 else CleanupFixture()
                try:
                    with sqlite3.connect(str(fixture.paths.database)) as connection:
                        if index == 0:
                            connection.execute(statement, (fixture.run_id,))
                        else:
                            connection.execute(statement, ("f" * 64, fixture.run_id))
                    result = RuntimeDataCleaner(fixture.paths).run(apply=True)
                    self.assertTrue((fixture.run_dir / "payload.profraw").is_file())
                    self.assertTrue((fixture.build_dir / "artifacts" / "dsym.zip").is_file())
                    self.assertFalse(result["planned_deletion"])
                    self.assertEqual(result["skipped"][0]["reason"], "latest_success_invalid")
                finally:
                    if fixture is not self.fixture:
                        fixture.close()

    def test_candidate_replaced_after_planning_is_rechecked_before_unlink(self):
        """Aborts before unlink when bytes change after planning and evidence publication."""

        cleaner = RuntimeDataCleaner(self.fixture.paths)
        original_publish = cleaner.evidence_store.publish
        profile = self.fixture.run_dir / "payload.profraw"

        def publish_then_replace(*arguments):
            evidence = original_publish(*arguments)
            profile.write_bytes(b"X" * len(self.fixture.payload))
            return evidence

        cleaner.evidence_store.publish = publish_then_replace
        with self.assertRaises(RuntimeCleanupError) as caught:
            cleaner.run(apply=True)
        self.assertEqual(caught.exception.code, "cleanup_candidate_identity_mismatch")
        self.assertTrue(profile.is_file())
        self.assertTrue((self.fixture.build_dir / "artifacts" / "dsym.zip").is_file())

    def test_file_replaced_after_final_preflight_is_preserved_in_quarantine(self):
        """Never unlinks replacement bytes inserted after the final public-path check."""

        cleaner = RuntimeDataCleaner(self.fixture.paths)
        profile = self.fixture.run_dir / "payload.profraw"
        backup = self.fixture.run_dir / "payload.profraw.original"
        replacement = b"X" * len(self.fixture.payload)
        original_verify = SecureRuntimeDeletion.verify_file
        profile_verify_calls = 0

        def verify_then_replace(secure, relative_path, expected_size, expected_sha256):
            nonlocal profile_verify_calls
            result = original_verify(
                secure, relative_path, expected_size, expected_sha256
            )
            if expected_sha256 == self.fixture.payload_sha:
                profile_verify_calls += 1
                if profile_verify_calls == 2:
                    profile.rename(backup)
                    profile.write_bytes(replacement)
            return result

        with patch.object(SecureRuntimeDeletion, "verify_file", verify_then_replace):
            with self.assertRaises(RuntimeCleanupError) as caught:
                cleaner.run(apply=True)
        self.assertEqual(caught.exception.code, "cleanup_candidate_identity_mismatch")
        self.assertEqual(backup.read_bytes(), self.fixture.payload)
        quarantined_files = [
            item
            for directory in self.fixture.paths.analysis_dir.glob(
                ".cleanup-quarantine-*"
            )
            for item in directory.iterdir()
            if item.is_file()
        ]
        self.assertEqual(len(quarantined_files), 1)
        self.assertEqual(quarantined_files[0].read_bytes(), replacement)

    def test_directory_changed_after_final_preflight_is_preserved_in_quarantine(self):
        """Does not recursively delete an unplanned file added to a directory candidate."""

        cleaner = RuntimeDataCleaner(self.fixture.paths)
        target = self.fixture.old_dir.relative_to(self.fixture.paths.root)
        original_verify = SecureRuntimeDeletion.verify_tree
        target_verify_calls = 0
        unexpected = self.fixture.old_dir / "unplanned.txt"

        def verify_then_add(secure, relative_path, expected_entries):
            nonlocal target_verify_calls
            result = original_verify(secure, relative_path, expected_entries)
            if relative_path == target:
                target_verify_calls += 1
                if target_verify_calls == 2:
                    unexpected.write_text("must be retained", encoding="utf-8")
            return result

        with patch.object(SecureRuntimeDeletion, "verify_tree", verify_then_add):
            with self.assertRaises(RuntimeCleanupError) as caught:
                cleaner.run(apply=True)
        self.assertEqual(caught.exception.code, "cleanup_candidate_identity_mismatch")
        quarantined_files = [
            item
            for directory in self.fixture.paths.analysis_dir.glob(
                ".cleanup-quarantine-*"
            )
            for item in directory.rglob("unplanned.txt")
        ]
        self.assertEqual(len(quarantined_files), 1)
        self.assertEqual(quarantined_files[0].read_text(), "must be retained")

    def test_ancestor_replaced_by_external_symlink_cannot_redirect_unlink(self):
        """Keeps an outside file when a planned ancestor becomes a matching symlink."""

        cleaner = RuntimeDataCleaner(self.fixture.paths)
        original_publish = cleaner.evidence_store.publish
        original_run_directory = self.fixture.run_dir
        displaced_run_directory = original_run_directory.with_name(
            original_run_directory.name + "-displaced"
        )
        external_directory = Path(self.fixture.temporary.name).parent / (
            "ios-coverage-cleanup-external-" + uuid.uuid4().hex
        )
        external_profile = external_directory / "payload.profraw"

        def publish_then_swap_ancestor(*arguments):
            evidence = original_publish(*arguments)
            external_directory.mkdir()
            external_profile.write_bytes(self.fixture.payload)
            original_run_directory.rename(displaced_run_directory)
            original_run_directory.symlink_to(external_directory, target_is_directory=True)
            return evidence

        cleaner.evidence_store.publish = publish_then_swap_ancestor
        try:
            with self.assertRaises(RuntimeCleanupError) as caught:
                cleaner.run(apply=True)
            self.assertEqual(caught.exception.code, "cleanup_path_changed")
            self.assertTrue(external_profile.is_file())
            self.assertEqual(external_profile.read_bytes(), self.fixture.payload)
        finally:
            if original_run_directory.is_symlink():
                original_run_directory.unlink()
            if displaced_run_directory.exists():
                displaced_run_directory.rename(original_run_directory)
            shutil.rmtree(external_directory, ignore_errors=True)

    def test_wrong_result_kind_invalidates_entire_latest_graph(self):
        """Rejects a hash-valid cumulative result used as the current diff reference."""

        results_path = self.fixture.orchestration_dir / "results.json"
        results = json.loads(results_path.read_text())
        results["current_diff_result"] = dict(results["cycle_coverage_result"])
        self.fixture._write_json(results_path, results)
        self.fixture._complete(
            self.fixture.orchestration_dir, "complete.json",
            "orchestration_input_sha256", self.fixture.orchestration_sha,
        )
        result = RuntimeDataCleaner(self.fixture.paths).run()
        self.assertFalse(result["planned_deletion"])
        self.assertEqual(result["skipped"][0]["reason"], "latest_success_invalid")

    def test_identity_mismatched_old_generation_is_retained(self):
        """Does not delete a completion-valid directory whose identity differs from its path."""

        directory_sha = "c" * 64
        claimed_sha = "d" * 64
        bad = self.fixture.paths.analysis_dir / "cycle-latest" / self.fixture.cycle_id / directory_sha
        bad.mkdir(parents=True)
        self.fixture._write_json(bad / "manifest.json", {
            "schema_version": 1, "cycle": {"cycle_id": self.fixture.cycle_id},
            "current_build_id": self.fixture.build_id,
        })
        self.fixture._write_json(bad / "summary.json", {
            "schema_version": 1, "status": "complete", "cycle_id": self.fixture.cycle_id,
            "current_build_id": self.fixture.build_id,
            "orchestration_input_sha256": claimed_sha,
        })
        self.fixture._complete(bad, "complete.json", "orchestration_input_sha256", claimed_sha)
        RuntimeDataCleaner(self.fixture.paths).run(apply=True)
        self.assertTrue(bad.is_dir())

    def test_misplaced_evidence_is_rejected_by_cycle_and_build_scope(self):
        """Never accepts a copied evidence record under a different Build parent."""

        RuntimeDataCleaner(self.fixture.paths).run(apply=True)
        store = RetainedBuildEvidenceStore(self.fixture.paths.analysis_dir)
        source = store.evidence_directory(
            self.fixture.cycle_id, self.fixture.build_id, self.fixture.input_sha
        )
        other_build = str(uuid.uuid4())
        misplaced = store.evidence_directory(
            self.fixture.cycle_id, other_build, self.fixture.input_sha
        )
        misplaced.parent.mkdir(parents=True)
        shutil.copytree(source, misplaced)
        run = {
            "id": self.fixture.run_id, "cycle_id": self.fixture.cycle_id,
            "build_id": other_build, "payload_size": 15,
            "payload_sha256": "7" * 64, "metadata_sha256": "6" * 64,
        }
        self.assertFalse(store.run_is_retained(self.fixture.cycle_id, other_build, run))
        self.assertNotIn(misplaced, {
            store.evidence_directory(item.cycle_id, item.build_id, item.input_set_sha256)
            for item in store.valid_evidence()
        })

    def test_obsolete_valid_evidence_is_removed_with_its_old_result(self):
        """Bounds normal retained-evidence growth across successful cleanup generations."""

        old_input = "a" * 64
        old_result = self.fixture.paths.analysis_dir / "single-build" / self.fixture.build_id / old_input
        shutil.copytree(self.fixture.single_dir, old_result)
        inputs = json.loads((old_result / "inputs.json").read_text())
        inputs["input_set_sha256"] = old_input
        self.fixture._write_json(old_result / "inputs.json", inputs)
        summary = json.loads((old_result / "summary.json").read_text())
        summary["input_set_sha256"] = old_input
        self.fixture._write_json(old_result / "summary.json", summary)
        self.fixture._complete(old_result, "completion.json", "input_set_sha256", old_input)
        store = RetainedBuildEvidenceStore(self.fixture.paths.analysis_dir)
        obsolete = store.evidence_directory(
            self.fixture.cycle_id, self.fixture.build_id, old_input
        )
        store.publish(self.fixture.cycle_id, self.fixture.build_id, old_result, ["dsym"])

        RuntimeDataCleaner(self.fixture.paths).run(apply=True)
        self.assertFalse(old_result.exists())
        self.assertFalse(obsolete.exists())

    def test_interruption_removes_obsolete_evidence_before_its_source_result(self):
        """Avoids an unverifiable evidence orphan when deletion stops partway through."""

        old_input = "a" * 64
        old_result = self.fixture.paths.analysis_dir / "single-build" / self.fixture.build_id / old_input
        shutil.copytree(self.fixture.single_dir, old_result)
        inputs = json.loads((old_result / "inputs.json").read_text())
        inputs["input_set_sha256"] = old_input
        self.fixture._write_json(old_result / "inputs.json", inputs)
        summary = json.loads((old_result / "summary.json").read_text())
        summary["input_set_sha256"] = old_input
        self.fixture._write_json(old_result / "summary.json", summary)
        self.fixture._complete(old_result, "completion.json", "input_set_sha256", old_input)
        store = RetainedBuildEvidenceStore(self.fixture.paths.analysis_dir)
        obsolete = store.evidence_directory(
            self.fixture.cycle_id, self.fixture.build_id, old_input
        )
        store.publish(self.fixture.cycle_id, self.fixture.build_id, old_result, ["dsym"])
        original_delete_tree = SecureRuntimeDeletion.delete_tree
        deleted = []

        def delete_then_interrupt(secure, relative_path, expected_entries):
            if deleted:
                raise SecureDeletionError(
                    "cleanup_delete_failed", "simulated cleanup interruption"
                )
            deleted.append(relative_path)
            return original_delete_tree(secure, relative_path, expected_entries)

        with patch.object(SecureRuntimeDeletion, "delete_tree", delete_then_interrupt):
            with self.assertRaises(RuntimeCleanupError):
                RuntimeDataCleaner(self.fixture.paths).run(apply=True)
        self.assertIn("retained-build", deleted[0].parts)
        self.assertFalse(obsolete.exists())
        self.assertTrue(old_result.is_dir())

    def test_debug_dylib_coverage_binary_is_retained_by_logical_role(self):
        """Keeps the selected dylib while removing the noncoverage executable and dSYM."""

        fixture = CleanupFixture("debug_dylib")
        try:
            RuntimeDataCleaner(fixture.paths).run(apply=True)
            artifacts = fixture.build_dir / "artifacts"
            self.assertTrue((artifacts / "debug_dylib.bin").is_file())
            self.assertFalse((artifacts / "main_executable.bin").exists())
            self.assertFalse((artifacts / "dsym.zip").exists())
        finally:
            fixture.close()

    def test_tampered_reference_is_conservatively_retained(self):
        (self.fixture.single_dir / "merged.profdata").write_bytes(b"changed")
        result = RuntimeDataCleaner(self.fixture.paths).run()
        self.assertTrue(result["skipped"])
        self.assertFalse(result["planned_deletion"])

    def test_external_symlink_rejects_apply_without_following_it(self):
        external = Path(self.fixture.temporary.name).parent / ("external-" + uuid.uuid4().hex)
        external.write_text("outside")
        link = self.fixture.paths.data_dir / "external-link"
        link.symlink_to(external)
        try:
            with self.assertRaises(RuntimeCleanupError) as caught:
                RuntimeDataCleaner(self.fixture.paths).run(apply=True)
            self.assertEqual(caught.exception.code, "unsafe_runtime_symlink")
            self.assertEqual(external.read_text(), "outside")
        finally:
            link.unlink(missing_ok=True)
            external.unlink(missing_ok=True)

    def test_unincorporated_run_payload_is_retained(self):
        """Never deletes a newer Run that is absent from the latest retained baseline."""

        run_id = str(uuid.uuid4())
        run_dir = self.fixture.paths.data_dir / "cycles" / self.fixture.cycle_id / "runs" / run_id
        run_dir.mkdir(parents=True)
        (run_dir / "metadata.json").write_text("{}\n")
        (run_dir / "payload.profraw").write_bytes(b"new-unincorporated-profile")
        (run_dir / "upload.sha256").write_text("c" * 64 + "\n")
        with sqlite3.connect(str(self.fixture.paths.database)) as connection:
            connection.execute(
                "INSERT INTO runs VALUES (?, ?, ?, ?, ?, ?, 'uploaded', 1, NULL)",
                (run_id, self.fixture.cycle_id, self.fixture.build_id, "b" * 64, "c" * 64, 26),
            )
        RuntimeDataCleaner(self.fixture.paths).run(apply=True)
        self.assertTrue((run_dir / "payload.profraw").is_file())

    def test_failed_new_generation_keeps_latest_success_readable(self):
        """Compacts from the prior success without treating a failed attempt as a report."""

        self.fixture.state_store.request_analysis(self.fixture.cycle_id, "manual_retry")
        self.fixture.state_store.claim(self.fixture.cycle_id)
        self.fixture.state_store.complete_failure(
            self.fixture.cycle_id, 2, "single_build", "fixture_failure", "fixture failure"
        )
        RuntimeDataCleaner(self.fixture.paths).run(apply=True)
        report = CoverageReportStore(
            self.fixture.paths.analysis_dir, AnalysisStateStore(self.fixture.paths.database)
        ).get_report(self.fixture.cycle_id)
        self.assertEqual(report["generation"], 1)

    def test_incomplete_analysis_directory_is_conservatively_retained(self):
        """Ignores a directory without a completion proof instead of guessing it is obsolete."""

        incomplete = self.fixture.paths.analysis_dir / "cycle-latest" / self.fixture.cycle_id / ("d" * 64)
        incomplete.mkdir(parents=True)
        (incomplete / "partial.json").write_text("{}\n")
        RuntimeDataCleaner(self.fixture.paths).run(apply=True)
        self.assertTrue((incomplete / "partial.json").is_file())

    def test_escaped_result_reference_keeps_cycle_data(self):
        """Rejects a latest-success pointer outside runtime_root and plans no Cycle deletion."""

        results_path = self.fixture.orchestration_dir / "results.json"
        results = json.loads(results_path.read_text())
        results["single_build_results"][0]["output_directory"] = "/private/tmp/outside"
        self.fixture._write_json(results_path, results)
        self.fixture._complete(
            self.fixture.orchestration_dir, "complete.json",
            "orchestration_input_sha256", self.fixture.orchestration_sha,
        )
        result = RuntimeDataCleaner(self.fixture.paths).run()
        self.assertFalse(result["planned_deletion"])
        self.assertTrue(result["skipped"])

    def test_cli_defaults_to_dry_run_and_requires_explicit_apply(self):
        """Keeps the destructive choice explicit and exposes no arbitrary path argument."""

        self.assertFalse(build_parser().parse_args([]).apply)
        self.assertTrue(build_parser().parse_args(["--apply"]).apply)
        with self.assertRaises(SystemExit):
            build_parser().parse_args(["--runtime-root", "/tmp/example"])


if __name__ == "__main__":
    unittest.main()
