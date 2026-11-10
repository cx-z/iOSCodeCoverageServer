#!/usr/bin/env python3
"""Focused tests for deterministic, snapshot-based Cycle orchestration."""

import hashlib
import json
import sqlite3
import tempfile
import unittest
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import ioscoverage.analysis.orchestrator as orchestrator_module
from ioscoverage.cli.analyze_cycle_latest import build_parser
from ioscoverage.analysis.orchestrator import (
    CycleAnalysisOrchestrationError,
    CycleAnalysisOrchestrator,
)
from ioscoverage.analysis.cumulative.analyzer import CycleCoverageAnalysisError
from ioscoverage.analysis.single_build.analyzer import AnalysisConfiguration, SingleBuildAnalysisError
from ioscoverage.analysis.diff.analyzer import SingleBuildDiffAnalysisError


class FakeSingleBuildAnalyzer:
    """Publishes tiny deterministic stand-ins while recording exact Run selection."""

    def __init__(self, root: Path):
        self.root = root / "single-build"
        self.calls = []
        self.failure_by_build = {}

    def analyze(self, cycle_id, build_id, run_ids):
        """Returns a completion-protected identity derived only from explicit inputs."""

        self.calls.append((cycle_id, build_id, tuple(run_ids)))
        if build_id in self.failure_by_build:
            raise self.failure_by_build[build_id]
        identity = sha({"cycle": cycle_id, "build": build_id, "runs": list(run_ids)})
        directory = self.root / build_id / identity
        publish_fake_result(directory, "completion.json", "input_set_sha256", identity)
        return SimpleNamespace(
            input_set_sha256=identity,
            output_directory=directory,
            summary={"status": "complete", "input_set_sha256": identity},
            idempotent_replay=len(self.calls) > 1,
        )


class FakeDiffAnalyzer:
    """Publishes a deterministic diff result for the chosen current Build."""

    def __init__(self, root: Path):
        self.root = root / "single-build-diff"
        self.calls = []
        self.failure = None

    def analyze(self, cycle_id, build_id, input_set_sha256):
        """Returns one immutable diff identity without interpreting coverage fixtures."""

        self.calls.append((cycle_id, build_id, input_set_sha256))
        if self.failure is not None:
            raise self.failure
        identity = sha({"build": build_id, "input": input_set_sha256})
        directory = self.root / build_id / input_set_sha256 / identity
        publish_fake_result(directory, "completion.json", "diff_set_sha256", identity)
        return SimpleNamespace(
            diff_set_sha256=identity,
            output_directory=directory,
            summary={"status": "complete", "diff_set_sha256": identity},
            idempotent_replay=False,
        )


class FakeCycleAnalyzer:
    """Publishes a deterministic cumulative result without duplicating its algorithm."""

    def __init__(self, root: Path):
        self.root = root / "cycle-coverage"
        self.calls = []
        self.failure = None
        self.lines = {"covered": 9, "uncovered": 1, "measurable": 10}

    def analyze(self, cycle_id, current_build_id, build_inputs):
        """Returns a stable cumulative summary from exact Build input identities."""

        self.calls.append((cycle_id, current_build_id, dict(build_inputs)))
        if self.failure is not None:
            raise self.failure
        identity = sha(
            {"cycle": cycle_id, "current": current_build_id, "inputs": dict(build_inputs)}
        )
        directory = self.root / cycle_id / current_build_id / identity
        summary = {
            "status": "complete",
            "cycle_input_set_sha256": identity,
            "lines": self.lines,
        }
        publish_fake_result(
            directory, "complete.json", "cycle_input_set_sha256", identity, summary
        )
        return SimpleNamespace(
            cycle_input_set_sha256=identity,
            output_directory=directory,
            summary=summary,
            idempotent_replay=False,
        )


class OrchestrationWorkspace:
    """Owns a temporary database and immutable Build/Run byte layout."""

    cycle_id = "11111111-1111-4111-8111-111111111111"
    build_a = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
    build_b = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
    build_c = "cccccccc-cccc-4ccc-8ccc-cccccccccccc"
    build_empty = "dddddddd-dddd-4ddd-8ddd-dddddddddddd"
    sha_a = "a" * 40
    sha_b = "b" * 40
    sha_c = "c" * 40
    target_sha = "d" * 40
    merge_base = "e" * 40

    def __init__(self):
        """Creates three analyzable Builds and one intentionally empty Build."""

        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.database = self.root / "instance" / "coverage.sqlite3"
        self.data = self.root / "data"
        self.analysis = self.root / "analysis"
        self.repository = self.root / "repository"
        self.repository.mkdir()
        self.database.parent.mkdir()
        self.configuration = AnalysisConfiguration(
            database_path=self.database,
            data_dir=self.data,
            analysis_root=self.analysis / "single-build",
            repository_path=self.repository,
        )
        self._create_schema()
        self.add_build(self.build_empty, self.sha_a, "2026-01-01T00:00:00Z")
        self.add_build(self.build_a, self.sha_a, "2026-01-02T00:00:00Z")
        self.add_build(self.build_b, self.sha_b, "2026-01-03T00:00:00Z")
        self.add_build(self.build_c, self.sha_c, "2026-01-04T00:00:00Z")
        self.add_run(self.build_a, "10000000-0000-4000-8000-000000000001", b"A1")
        self.add_run(self.build_b, "20000000-0000-4000-8000-000000000001", b"B1")
        self.add_run(self.build_c, "30000000-0000-4000-8000-000000000001", b"C1")

    def close(self):
        """Releases the temporary workspace."""

        self.temporary.cleanup()

    def orchestrator(self, observer=None):
        """Builds one orchestrator backed by controllable downstream analyzers."""

        single = FakeSingleBuildAnalyzer(self.analysis)
        diff = FakeDiffAnalyzer(self.analysis)
        cycle = FakeCycleAnalyzer(self.analysis)
        return (
            CycleAnalysisOrchestrator(
                self.configuration, single, diff, cycle, snapshot_observer=observer
            ),
            single,
            diff,
            cycle,
        )

    def add_build(self, build_id, git_sha, created_at, state="ready"):
        """Adds one immutable Build manifest and database identity."""

        manifest = {
            "schema_version": 1,
            "build_id": build_id,
            "cycle_id": self.cycle_id,
            "git_sha": git_sha,
            "coverage_binary": "main_executable",
        }
        manifest_sha = sha(manifest)
        build_dir = self.data / "cycles" / self.cycle_id / "builds" / build_id
        build_dir.mkdir(parents=True)
        write_json(build_dir / "manifest.json", manifest)
        values = (
            build_id,
            self.cycle_id,
            1,
            git_sha,
            self.target_sha,
            self.merge_base,
            "main_executable",
            "com.example.coverage",
            "1.0",
            "1",
            "26.3",
            "6",
            "[]",
            None,
            "[]",
            manifest_sha,
            canonical(manifest).decode(),
            "1" * 64,
            10,
            None,
            None,
            "2" * 64,
            20,
            state,
            0,
            created_at,
        )
        with sqlite3.connect(str(self.database)) as connection:
            connection.execute(
                "INSERT INTO builds VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                values,
            )

    def add_run(
        self,
        build_id,
        run_id,
        payload,
        state="uploaded",
        analyzable=1,
        error_message=None,
        cycle_id=None,
    ):
        """Adds one Run and its exact metadata/profraw bytes."""

        owner_cycle = cycle_id or self.cycle_id
        metadata = {
            "schema_version": 1,
            "run_id": run_id,
            "cycle_id": owner_cycle,
            "build_id": build_id,
        }
        metadata_sha = sha(metadata)
        payload_sha = hashlib.sha256(payload).hexdigest()
        run_dir = self.data / "cycles" / self.cycle_id / "runs" / run_id
        run_dir.mkdir(parents=True)
        write_json(run_dir / "metadata.json", metadata)
        (run_dir / "payload.profraw").write_bytes(payload)
        with sqlite3.connect(str(self.database)) as connection:
            connection.execute(
                "INSERT INTO runs VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    run_id,
                    owner_cycle,
                    build_id,
                    1,
                    "device",
                    "process",
                    "launch",
                    "2026-01-01T00:00:00Z",
                    "2026-01-01T00:00:00Z",
                    metadata_sha,
                    canonical(metadata).decode(),
                    payload_sha,
                    len(payload),
                    state,
                    analyzable,
                    error_message,
                ),
            )

    def _create_schema(self):
        """Creates only the production columns needed by orchestration and analyzers."""

        with sqlite3.connect(str(self.database)) as connection:
            connection.executescript(
                """
                CREATE TABLE coverage_cycles (
                  id TEXT PRIMARY KEY, schema_version INTEGER, project TEXT, mr_iid TEXT,
                  mr_url TEXT, target_branch TEXT, base_sha TEXT, head_sha TEXT, status TEXT,
                  created_at TEXT, updated_at TEXT);
                CREATE TABLE builds (
                  id TEXT PRIMARY KEY, cycle_id TEXT, schema_version INTEGER, git_sha TEXT,
                  target_sha_at_build TEXT, merge_base_sha TEXT, coverage_binary TEXT,
                  bundle_id TEXT, app_version TEXT, bundle_version TEXT, xcode_version TEXT,
                  swift_version TEXT, main_executable_uuids_json TEXT,
                  debug_dylib_uuids_json TEXT, dsym_uuids_json TEXT, manifest_sha256 TEXT,
                  manifest_json TEXT, main_executable_sha256 TEXT, main_executable_size INTEGER,
                  debug_dylib_sha256 TEXT, debug_dylib_size INTEGER, dsym_sha256 TEXT,
                  dsym_size INTEGER, artifact_state TEXT, pending_runs_promoted INTEGER,
                  created_at TEXT);
                CREATE TABLE runs (
                  id TEXT PRIMARY KEY, cycle_id TEXT, build_id TEXT, schema_version INTEGER,
                  device_id TEXT, process_id TEXT, launch_id TEXT, captured_at TEXT,
                  received_at TEXT, metadata_sha256 TEXT, metadata_json TEXT,
                  payload_sha256 TEXT, payload_size INTEGER, state TEXT, analyzable INTEGER,
                  error_message TEXT);
                """
            )
            connection.execute(
                "INSERT INTO coverage_cycles VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (
                    self.cycle_id,
                    1,
                    "ExampleApp",
                    "56",
                    None,
                    "master",
                    "f" * 40,
                    self.sha_c,
                    "active",
                    "2026-01-01T00:00:00Z",
                    "2026-01-04T00:00:00Z",
                ),
            )


class CycleAnalysisOrchestratorTests(unittest.TestCase):
    """Exercises selection, snapshots, failure propagation, and immutable publication."""

    def setUp(self):
        """Creates a fresh isolated Cycle for every test."""

        self.workspace = OrchestrationWorkspace()

    def tearDown(self):
        """Removes every temporary analysis result."""

        self.workspace.close()

    def test_current_head_can_analyze_all_historical_builds(self):
        """Selects A/B/C even though current source checkout conceptually represents C."""

        orchestrator, single, _, _ = self.workspace.orchestrator()
        result = orchestrator.analyze(self.workspace.cycle_id)
        self.assertEqual([call[1] for call in single.calls], [self.workspace.build_a, self.workspace.build_b, self.workspace.build_c])
        self.assertEqual(result.summary["selected_build_count"], 3)

    def test_current_build_selection_uses_cycle_head_not_checkout(self):
        """Chooses C solely because its immutable git_sha equals Cycle.head_sha."""

        orchestrator, _, diff, _ = self.workspace.orchestrator()
        result = orchestrator.analyze(self.workspace.cycle_id)
        self.assertEqual(result.summary["current_build_id"], self.workspace.build_c)
        self.assertEqual(diff.calls[0][1], self.workspace.build_c)

    def test_dirty_worktree_marker_does_not_change_identity_or_summary(self):
        """Leaves unrelated uncommitted bytes outside orchestration inputs."""

        orchestrator, _, _, _ = self.workspace.orchestrator()
        first = orchestrator.analyze(self.workspace.cycle_id)
        marker = self.workspace.repository / "dirty.swift"
        marker.write_text("uncommitted", encoding="utf-8")
        second_orchestrator, _, _, _ = self.workspace.orchestrator()
        second = second_orchestrator.analyze(self.workspace.cycle_id)
        self.assertEqual(first.orchestration_input_sha256, second.orchestration_input_sha256)
        self.assertEqual(first.summary, second.summary)
        self.assertEqual(marker.read_text(encoding="utf-8"), "uncommitted")

    def test_same_path_worktree_source_is_never_read(self):
        """Does not include a same-path mutable source file in result identity."""

        source = self.workspace.repository / "Sources" / "Feature.swift"
        source.parent.mkdir()
        source.write_text("old", encoding="utf-8")
        first = self.workspace.orchestrator()[0].analyze(self.workspace.cycle_id)
        source.write_text("new dirty bytes", encoding="utf-8")
        second = self.workspace.orchestrator()[0].analyze(self.workspace.cycle_id)
        self.assertEqual(first.orchestration_input_sha256, second.orchestration_input_sha256)

    def test_missing_build_commit_object_failure_propagates(self):
        """Preserves immutable Git object rejection from the single-Build analyzer."""

        orchestrator, single, _, _ = self.workspace.orchestrator()
        single.failure_by_build[self.workspace.build_a] = SingleBuildAnalysisError("git_object_missing", "missing")
        self._assert_failure(orchestrator, "single_build", "git_object_missing")

    def test_wrong_merge_base_failure_propagates(self):
        """Preserves exact merge-base validation without consulting a branch."""

        orchestrator, single, _, _ = self.workspace.orchestrator()
        single.failure_by_build[self.workspace.build_a] = SingleBuildAnalysisError("merge_base_mismatch", "wrong")
        self._assert_failure(orchestrator, "single_build", "merge_base_mismatch")

    def test_wrong_fixed_repository_failure_propagates(self):
        """Preserves configured repository-root validation as a fatal input failure."""

        orchestrator, single, _, _ = self.workspace.orchestrator()
        single.failure_by_build[self.workspace.build_a] = SingleBuildAnalysisError("repository_root_mismatch", "wrong")
        self._assert_failure(orchestrator, "single_build", "repository_root_mismatch")

    def test_multiple_current_builds_use_created_at_then_build_id(self):
        """Selects the lexically latest Build after the latest matching timestamp."""

        later = "eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee"
        self.workspace.add_build(later, self.workspace.sha_c, "2026-01-04T00:00:00Z")
        self.workspace.add_run(later, "40000000-0000-4000-8000-000000000001", b"D1")
        result = self.workspace.orchestrator()[0].analyze(self.workspace.cycle_id)
        self.assertEqual(result.summary["current_build_id"], later)

    def test_missing_current_build_is_rejected(self):
        """Fails when no ready Build matches the immutable Cycle head."""

        with sqlite3.connect(str(self.workspace.database)) as connection:
            connection.execute("UPDATE coverage_cycles SET head_sha = ?", ("9" * 40,))
        self._assert_failure(self.workspace.orchestrator()[0], "build_selection", "current_build_not_found")

    def test_no_run_build_records_skip_reason(self):
        """Keeps the empty Build visible as no_analyzable_runs evidence."""

        result = self.workspace.orchestrator()[0].analyze(self.workspace.cycle_id)
        document = json.loads((result.output_directory / "selected-builds.json").read_text())
        empty = next(item for item in document["builds"] if item["build_id"] == self.workspace.build_empty)
        self.assertEqual(empty["skip_reason"], "no_analyzable_runs")

    def test_all_valid_runs_are_sorted_and_selected(self):
        """Passes every valid Run to its Build in stable run_id order."""

        extra = "10000000-0000-4000-8000-000000000000"
        self.workspace.add_run(self.workspace.build_a, extra, b"A0")
        _, single, _, _ = self.workspace.orchestrator()
        self.workspace.orchestrator()
        orchestrator = CycleAnalysisOrchestrator(self.workspace.configuration, single, FakeDiffAnalyzer(self.workspace.analysis), FakeCycleAnalyzer(self.workspace.analysis))
        orchestrator.analyze(self.workspace.cycle_id)
        a_call = next(call for call in single.calls if call[1] == self.workspace.build_a)
        self.assertEqual(list(a_call[2]), sorted(a_call[2]))
        self.assertEqual(len(a_call[2]), 2)

    def test_later_uploaded_run_changes_next_identity(self):
        """Creates a new immutable orchestration namespace when a Run arrives later."""

        first = self.workspace.orchestrator()[0].analyze(self.workspace.cycle_id)
        self.workspace.add_run(self.workspace.build_a, "10000000-0000-4000-8000-000000000002", b"A2")
        second = self.workspace.orchestrator()[0].analyze(self.workspace.cycle_id)
        self.assertNotEqual(first.orchestration_input_sha256, second.orchestration_input_sha256)

    def test_run_added_after_snapshot_is_not_mixed(self):
        """Uses only rows frozen before the observer simulates a concurrent upload."""

        def add_after_snapshot(_snapshot):
            self.workspace.add_run(self.workspace.build_a, "10000000-0000-4000-8000-000000000003", b"A3")

        orchestrator, single, _, _ = self.workspace.orchestrator(add_after_snapshot)
        first = orchestrator.analyze(self.workspace.cycle_id)
        a_call = next(call for call in single.calls if call[1] == self.workspace.build_a)
        self.assertEqual(len(a_call[2]), 1)
        second = self.workspace.orchestrator()[0].analyze(self.workspace.cycle_id)
        self.assertNotEqual(first.orchestration_input_sha256, second.orchestration_input_sha256)

    def test_run_ownership_mismatch_is_rejected(self):
        """Rejects a Run whose Cycle and Build ownership disagree in SQLite."""

        with sqlite3.connect(str(self.workspace.database)) as connection:
            connection.execute(
                "UPDATE runs SET cycle_id = ? WHERE build_id = ?", (str(uuid.uuid4()), self.workspace.build_a)
            )
        self._assert_failure(self.workspace.orchestrator()[0], "build_selection", "run_ownership_mismatch")

    def test_run_metadata_damage_is_rejected(self):
        """Fails the whole orchestration before any downstream analysis on metadata drift."""

        path = next((self.workspace.data / "cycles" / self.workspace.cycle_id / "runs").glob("*/metadata.json"))
        path.write_text("{}\n", encoding="utf-8")
        self._assert_failure(self.workspace.orchestrator()[0], "run_validation", "run_metadata_mismatch")

    def test_profraw_size_or_sha_damage_is_rejected(self):
        """Fails rather than skipping a selected corrupt Run."""

        path = next((self.workspace.data / "cycles" / self.workspace.cycle_id / "runs").glob("*/payload.profraw"))
        path.write_bytes(b"changed")
        self._assert_failure(self.workspace.orchestrator()[0], "run_validation", "profraw_identity_mismatch")

    def test_retained_baseline_is_resolved_once_per_build(self):
        """Avoids repeatedly hashing one large retained export for every missing Run."""

        second_run = "10000000-0000-4000-8000-000000000002"
        self.workspace.add_run(self.workspace.build_a, second_run, b"A2")
        run_root = self.workspace.data / "cycles" / self.workspace.cycle_id / "runs"
        for run_id in ("10000000-0000-4000-8000-000000000001", second_run):
            (run_root / run_id / "payload.profraw").unlink()

        class CountingEvidenceStore:
            def __init__(self):
                self.find_calls = []
                self.match_calls = []

            def find_compatible(self, cycle_id, build_id, manifest_sha256, runs):
                self.find_calls.append((build_id, tuple(run["id"] for run in runs)))
                return object() if build_id == OrchestrationWorkspace.build_a else None

            def run_matches(self, evidence, run):
                self.match_calls.append(run["id"])
                return True

        orchestrator = self.workspace.orchestrator()[0]
        evidence = CountingEvidenceStore()
        orchestrator.retained_evidence_store = evidence
        snapshot = orchestrator._load_snapshot(self.workspace.cycle_id)
        selection = orchestrator._select_inputs(snapshot)
        orchestrator._validate_selected_files(selection)

        self.assertEqual(
            sum(call[0] == self.workspace.build_a for call in evidence.find_calls), 1
        )
        self.assertEqual(len(evidence.match_calls), 2)

    def test_build_manifest_damage_is_rejected(self):
        """Rejects Build bytes that no longer match immutable SQLite identity."""

        path = self.workspace.data / "cycles" / self.workspace.cycle_id / "builds" / self.workspace.build_a / "manifest.json"
        path.write_text("{}\n", encoding="utf-8")
        self._assert_failure(self.workspace.orchestrator()[0], "run_validation", "build_manifest_mismatch")

    def test_historical_single_build_failure_prevents_cumulative_publication(self):
        """Does not call the cumulative analyzer after one selected historical Build fails."""

        orchestrator, single, _, cycle = self.workspace.orchestrator()
        single.failure_by_build[self.workspace.build_b] = SingleBuildAnalysisError("artifact_sha256_mismatch", "bad")
        self._assert_failure(orchestrator, "single_build", "artifact_sha256_mismatch")
        self.assertEqual(cycle.calls, [])

    def test_profiles_are_never_combined_across_build_calls(self):
        """Coordinates one separate single-Build invocation per Build."""

        orchestrator, single, _, _ = self.workspace.orchestrator()
        orchestrator.analyze(self.workspace.cycle_id)
        self.assertEqual(len(single.calls), 3)
        self.assertEqual(len({call[1] for call in single.calls}), 3)

    def test_downstream_summary_is_preserved_in_orchestration_summary(self):
        """References the existing cumulative algorithm output without reinterpreting it."""

        result = self.workspace.orchestrator()[0].analyze(self.workspace.cycle_id)
        self.assertEqual(result.summary["cumulative_summary"]["lines"], {"covered": 9, "uncovered": 1, "measurable": 10})

    def test_identical_input_is_idempotent(self):
        """Reuses exact orchestration and downstream references on the second call."""

        first = self.workspace.orchestrator()[0].analyze(self.workspace.cycle_id)
        second = self.workspace.orchestrator()[0].analyze(self.workspace.cycle_id)
        self.assertEqual(first.orchestration_input_sha256, second.orchestration_input_sha256)
        self.assertTrue(second.idempotent_replay)
        self.assertEqual(first.summary, second.summary)

    def test_retry_recomputes_old_version_result_with_unchanged_inputs(self):
        """An old completed Cycle cannot bypass the repaired diff and cumulative stages."""

        old_orchestrator, _, _, old_cycle = self.workspace.orchestrator()
        old_cycle.lines = {"covered": 0, "uncovered": 0, "measurable": 0,
                           "not_measurable": 392}
        with patch.object(orchestrator_module, "ORCHESTRATOR_VERSION", "cycle-latest/1"):
            old = old_orchestrator.analyze(self.workspace.cycle_id)

        new_orchestrator, single, diff, cycle = self.workspace.orchestrator()
        new = new_orchestrator.analyze(self.workspace.cycle_id)

        self.assertNotEqual(old.orchestration_input_sha256, new.orchestration_input_sha256)
        self.assertFalse(new.idempotent_replay)
        self.assertEqual(len(single.calls), 3)
        self.assertEqual(len(diff.calls), 1)
        self.assertEqual(len(cycle.calls), 1)
        self.assertEqual(new.summary["cumulative_summary"]["lines"]["measurable"], 10)
        self.assertTrue(old.output_directory.is_dir())

    def test_compiler_allowlist_change_cannot_reuse_completed_cycle(self):
        """Rechecks the configured mapping policy even with unchanged Builds and Runs."""

        old_orchestrator, _, _, _ = self.workspace.orchestrator()
        old = old_orchestrator.analyze(self.workspace.cycle_id)
        new_orchestrator, _, diff, cycle = self.workspace.orchestrator()
        with patch.object(AnalysisConfiguration, "project_compiler_repository_names",
                          return_value=("another-checkout",)):
            new = new_orchestrator.analyze(self.workspace.cycle_id)
        self.assertNotEqual(old.orchestration_input_sha256, new.orchestration_input_sha256)
        self.assertFalse(new.idempotent_replay)
        self.assertEqual(len(diff.calls), 1)
        self.assertEqual(len(cycle.calls), 1)

    def test_failure_staging_never_contains_complete_marker(self):
        """Publishes failure evidence only beneath .failed."""

        orchestrator, _, diff, _ = self.workspace.orchestrator()
        diff.failure = SingleBuildDiffAnalysisError("git_command_failed", "failed")
        self._assert_failure(orchestrator, "current_diff", "git_command_failed")
        failures = list((self.workspace.analysis / "cycle-latest" / ".failed").rglob("failure.json"))
        self.assertEqual(len(failures), 1)
        self.assertFalse(any(path.name == "complete.json" for path in failures[0].parent.rglob("*")))

    def test_sqlite_and_data_bytes_are_read_only(self):
        """Changes only analysis output, never service persistence."""

        before = tree_identity([self.workspace.database, self.workspace.data])
        self.workspace.orchestrator()[0].analyze(self.workspace.cycle_id)
        self.assertEqual(before, tree_identity([self.workspace.database, self.workspace.data]))

    def test_orchestrator_source_contains_no_git_mutation_commands(self):
        """Keeps checkout/switch/reset/clean/stash/worktree outside the coordinator."""

        source = Path(orchestrator_module.__file__).read_text(encoding="utf-8")
        for command in ("git checkout", "git switch", "git reset", "git clean", "git stash", "git worktree"):
            self.assertNotIn(command, source)

    def test_cli_rejects_unknown_and_path_arguments(self):
        """Exposes runtime root plus Cycle identity and rejects repository paths."""

        parser = build_parser()
        with self.assertRaises(SystemExit):
            parser.parse_args(["--runtime-root", str(self.workspace.root), "--cycle-id", self.workspace.cycle_id, "--repository", "/tmp/repo"])
        parsed = parser.parse_args(["--runtime-root", str(self.workspace.root), "--cycle-id", self.workspace.cycle_id])
        self.assertEqual(parsed.cycle_id, self.workspace.cycle_id)

    def test_current_build_without_valid_run_is_rejected(self):
        """Never emits a current report when the chosen current Build lacks valid inputs."""

        with sqlite3.connect(str(self.workspace.database)) as connection:
            connection.execute("UPDATE runs SET analyzable = 0 WHERE build_id = ?", (self.workspace.build_c,))
        self._assert_failure(self.workspace.orchestrator()[0], "build_selection", "current_build_has_no_analyzable_runs")

    def test_future_build_is_recorded_as_skipped(self):
        """Excludes and explains a ready Build created after the selected current Build."""

        future = "ffffffff-ffff-4fff-8fff-ffffffffffff"
        self.workspace.add_build(future, "8" * 40, "2026-01-05T00:00:00Z")
        self.workspace.add_run(future, "50000000-0000-4000-8000-000000000001", b"F1")
        result = self.workspace.orchestrator()[0].analyze(self.workspace.cycle_id)
        builds = json.loads((result.output_directory / "selected-builds.json").read_text())["builds"]
        record = next(item for item in builds if item["build_id"] == future)
        self.assertEqual(record["skip_reason"], "after_current_build")

    def test_downstream_reference_tampering_breaks_replay(self):
        """Refuses to reuse orchestration evidence after a referenced summary changes."""

        result = self.workspace.orchestrator()[0].analyze(self.workspace.cycle_id)
        references = json.loads((result.output_directory / "results.json").read_text())
        target = Path(references["current_diff_result"]["output_directory"]) / "summary.json"
        target.write_text("{}\n", encoding="utf-8")
        self._assert_failure(self.workspace.orchestrator()[0], "publication", "existing_result_invalid")

    def _assert_failure(self, orchestrator, stage, code):
        """Asserts one stable failure contract."""

        with self.assertRaises(CycleAnalysisOrchestrationError) as caught:
            orchestrator.analyze(self.workspace.cycle_id)
        self.assertEqual(caught.exception.stage, stage)
        self.assertEqual(caught.exception.code, code)


def canonical(payload):
    """Returns canonical JSON bytes used by persisted SHA identities."""

    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def sha(payload):
    """Returns a canonical JSON SHA-256."""

    return hashlib.sha256(canonical(payload)).hexdigest()


def write_json(path, payload):
    """Writes deterministic fixture JSON."""

    path.write_text(json.dumps(payload, sort_keys=True, indent=2) + "\n", encoding="utf-8")


def publish_fake_result(directory, completion_name, identity_key, identity, summary=None):
    """Creates the two small downstream files required for reference auditing."""

    directory.mkdir(parents=True, exist_ok=True)
    write_json(directory / "summary.json", summary or {"status": "complete", identity_key: identity})
    write_json(directory / completion_name, {"status": "complete", identity_key: identity})


def tree_identity(paths):
    """Hashes a fixed file or tree collection without following symlinks."""

    result = {}
    for root in paths:
        candidates = [root] if root.is_file() else sorted(path for path in root.rglob("*") if path.is_file())
        for path in candidates:
            result[str(path)] = hashlib.sha256(path.read_bytes()).hexdigest()
    return result


if __name__ == "__main__":
    unittest.main()
