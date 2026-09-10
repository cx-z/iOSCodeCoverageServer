#!/usr/bin/env python3
"""Automatic-analysis persistence, scheduler, trigger, and API tests."""

import hashlib
import http.client
import io
import json
import logging
import sqlite3
import tempfile
import threading
import time
import unittest
import uuid
from pathlib import Path
from tests.helpers import FakeProjectRegistry
from types import SimpleNamespace

from ioscoverage.analysis.scheduler import AnalysisScheduler, AnalysisSchedulerError
from ioscoverage.lifecycle.runtime_lock import RuntimeLockError, RuntimeProcessLock
from ioscoverage.storage.analysis_state import AnalysisStateStore
from ioscoverage.storage.coverage_store import CoverageStore
from ioscoverage.analysis.orchestrator import CycleAnalysisOrchestrationError
from server import ProbeConfig, ProbeHTTPServer


class FakeRepository:
    """Accepts fixed Git identities for isolated HTTP persistence tests."""

    def __init__(self, path: Path):
        self.path = path

    def validate_build_snapshot(self, git_sha, target_sha_at_build, merge_base_sha):
        """Records no mutable checkout state and accepts fixture commits."""


class FakeArtifactValidator:
    """Returns declared UUIDs without requiring fixture Mach-O binaries."""

    def validate(self, artifact_paths, declared_uuids, coverage_binary, staging_dir, limit):
        """Preserves the exact declared artifact identity for protocol tests."""

        return declared_uuids


class ControlledOrchestratorFactory:
    """Produces controllable orchestration stand-ins and records worker concurrency."""

    def __init__(self, output_root: Path):
        self.output_root = output_root
        self.calls = []
        self.active = 0
        self.max_active = 0
        self.lock = threading.Lock()
        self.started = threading.Event()
        self.release = threading.Event()
        self.block_first = False
        self.block_next = False
        self.fail_next = False
        self.cancelled = False
        self.cancel_delay = 0.0

    def __call__(self):
        """Returns an object exposing the production analyzer's narrow method."""

        return SimpleNamespace(analyze=self.analyze)

    def analyze(self, cycle_id):
        """Blocks or fails on demand and otherwise returns stable orchestration output."""

        with self.lock:
            self.calls.append(cycle_id)
            call_number = len(self.calls)
            self.active += 1
            self.max_active = max(self.max_active, self.active)
            should_block = (self.block_first and call_number == 1) or self.block_next
            self.block_next = False
            should_fail = self.fail_next
            self.fail_next = False
        self.started.set()
        try:
            if should_block:
                if not self.release.wait(timeout=5):
                    raise RuntimeError("test analyzer release timed out")
            if self.cancelled:
                raise CycleAnalysisOrchestrationError(
                    "orchestration",
                    "analysis_cancelled",
                    "fixture analysis was cancelled",
                )
            if should_fail:
                raise CycleAnalysisOrchestrationError(
                    "single_build", "fixture_analysis_failed", "fixture analysis failed"
                )
            digest = hashlib.sha256(cycle_id.encode("ascii")).hexdigest()
            return SimpleNamespace(
                orchestration_input_sha256=digest,
                output_directory=self.output_root / cycle_id / digest,
                summary={
                    "current_build_id": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
                    "cycle_input_set_sha256": "b" * 64,
                    "cumulative_summary": {
                        "changed_line_coverage_percent": 60.0,
                        "lines": {
                            "changed_new_side": 20,
                            "measurable": 10,
                            "covered": 6,
                            "uncovered": 4,
                            "not_measurable": 10,
                            "unknown": 0,
                            "deleted_excluded": 2,
                            "covered_with_inherited_evidence": 3,
                        },
                    },
                },
            )
        finally:
            with self.lock:
                self.active -= 1

    def cancel_active(self):
        """Cooperatively releases the fixture while modeling production cancellation."""

        self.cancelled = True
        if self.cancel_delay:
            time.sleep(self.cancel_delay)
        self.release.set()


class StateWorkspace:
    """Creates a minimal existing v2 database for additive migration tests."""

    def __init__(self, cycle_count=2):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.database = self.root / "coverage.sqlite3"
        self.cycle_ids = [str(uuid.uuid4()) for _ in range(cycle_count)]
        with sqlite3.connect(str(self.database)) as connection:
            connection.executescript(
                """
                CREATE TABLE coverage_cycles (id TEXT PRIMARY KEY);
                CREATE TABLE builds (id TEXT PRIMARY KEY, cycle_id TEXT);
                CREATE TABLE runs (id TEXT PRIMARY KEY, cycle_id TEXT, build_id TEXT);
                PRAGMA user_version = 2;
                """
            )
            for cycle_id in self.cycle_ids:
                connection.execute("INSERT INTO coverage_cycles VALUES (?)", (cycle_id,))
            for index in range(4):
                connection.execute(
                    "INSERT INTO builds VALUES (?, ?)", (str(uuid.uuid4()), self.cycle_ids[0])
                )
            for index in range(17):
                connection.execute(
                    "INSERT INTO runs VALUES (?, ?, ?)",
                    (str(uuid.uuid4()), self.cycle_ids[0], str(uuid.uuid4())),
                )
        self.store = AnalysisStateStore(self.database)

    def close(self):
        """Releases the temporary state database."""

        self.temporary.cleanup()


class AnalysisStateStoreTests(unittest.TestCase):
    """Verifies additive migration and generation-conditional state transitions."""

    def setUp(self):
        """Creates two Cycles with pre-existing Build/Run counts."""

        self.workspace = StateWorkspace()
        self.store = self.workspace.store
        self.cycle = self.workspace.cycle_ids[0]

    def tearDown(self):
        """Removes temporary persisted state."""

        self.workspace.close()

    def test_migration_preserves_existing_builds_and_runs(self):
        """Adds only the analysis table and advances v2 to v3."""

        with sqlite3.connect(str(self.workspace.database)) as connection:
            self.assertEqual(connection.execute("SELECT count(*) FROM builds").fetchone()[0], 4)
            self.assertEqual(connection.execute("SELECT count(*) FROM runs").fetchone()[0], 17)
            self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 3)
            self.assertIsNotNone(
                connection.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='cycle_analysis_states'"
                ).fetchone()
            )

    def test_idle_state_is_explicit_and_get_does_not_insert(self):
        """Synthesizes generation zero without turning GET into a mutation."""

        state = self.store.get_state(self.cycle)
        self.assertEqual((state["status"], state["requested_generation"]), ("idle", 0))
        with sqlite3.connect(str(self.workspace.database)) as connection:
            self.assertEqual(connection.execute("SELECT count(*) FROM cycle_analysis_states").fetchone()[0], 0)

    def test_repeated_queued_requests_increment_and_coalesce(self):
        """Uses generations instead of one physical queue entry per request."""

        self.store.request_analysis(self.cycle, "manual_retry")
        state = self.store.request_analysis(self.cycle, "run_uploaded", str(uuid.uuid4()))
        self.assertEqual((state["status"], state["requested_generation"]), ("queued", 2))

    def test_claim_sets_one_running_generation_and_attempt(self):
        """Claims the latest generation atomically."""

        self.store.request_analysis(self.cycle, "manual_retry")
        self.assertEqual(self.store.claim(self.cycle), 1)
        state = self.store.get_state(self.cycle)
        self.assertEqual((state["status"], state["running_generation"], state["attempt_count"]), ("running", 1, 1))

    def test_request_during_running_preserves_current_and_advances_latest(self):
        """Keeps the in-flight generation while recording newer work."""

        self.store.request_analysis(self.cycle, "manual_retry")
        self.store.claim(self.cycle)
        state = self.store.request_analysis(self.cycle, "run_uploaded", str(uuid.uuid4()))
        self.assertEqual((state["status"], state["running_generation"], state["requested_generation"]), ("running", 1, 2))

    def test_success_with_newer_request_requeues_latest_snapshot(self):
        """Commits generation one but does not claim generation two completed."""

        self.store.request_analysis(self.cycle, "manual_retry")
        self.store.claim(self.cycle)
        self.store.request_analysis(self.cycle, "run_uploaded", str(uuid.uuid4()))
        state = self.store.complete_success(self.cycle, 1, success_document())
        self.assertEqual((state["status"], state["completed_generation"]), ("queued", 1))
        self.assertEqual(state["latest_success_generation"], 1)

    def test_latest_generation_success_becomes_succeeded(self):
        """Marks success only when no newer generation is pending."""

        self.store.request_analysis(self.cycle, "manual_retry")
        self.store.claim(self.cycle)
        state = self.store.complete_success(self.cycle, 1, success_document())
        self.assertEqual((state["status"], state["completed_generation"]), ("succeeded", 1))

    def test_failure_preserves_latest_success(self):
        """Retains the prior report while recording the latest safe failure."""

        self._complete_success()
        self.store.request_analysis(self.cycle, "manual_retry")
        self.store.claim(self.cycle)
        state = self.store.complete_failure(self.cycle, 2, "single_build", "bad_input", "bad input")
        self.assertEqual(state["status"], "failed")
        self.assertEqual(state["latest_success_generation"], 1)
        self.assertEqual(state["error_code"], "bad_input")

    def test_new_request_requeues_failed_state(self):
        """Allows a new valid Run or manual retry to recover from failure."""

        self.store.request_analysis(self.cycle, "manual_retry")
        self.store.claim(self.cycle)
        self.store.complete_failure(self.cycle, 1, "cumulative", "failed", "failed")
        state = self.store.request_analysis(self.cycle, "run_uploaded", str(uuid.uuid4()))
        self.assertEqual((state["status"], state["requested_generation"]), ("queued", 2))
        self.assertIsNone(state["error_code"])

    def test_running_recovery_is_queued_once(self):
        """Records one startup recovery without incrementing generations or looping."""

        self.store.request_analysis(self.cycle, "manual_retry")
        self.store.claim(self.cycle)
        self.assertEqual(self.store.recover_interrupted(), [self.cycle])
        self.assertEqual(self.store.recover_interrupted(), [])
        state = self.store.get_state(self.cycle)
        self.assertEqual((state["status"], state["recovery_count"]), ("queued", 1))

    def test_stale_generation_cannot_overwrite_newer_completion(self):
        """Rejects a late result unless it owns the exact running generation."""

        self._complete_success()
        stale = self.store.complete_failure(self.cycle, 1, "late", "late", "late")
        self.assertEqual(stale["status"], "succeeded")
        self.assertIsNone(stale["error_code"])

    def test_unknown_cycle_is_rejected(self):
        """Does not create orphan scheduling rows."""

        with self.assertRaisesRegex(Exception, "not registered"):
            self.store.request_analysis(str(uuid.uuid4()), "manual_retry")

    def _complete_success(self):
        """Creates one prior successful generation for failure tests."""

        self.store.request_analysis(self.cycle, "manual_retry")
        self.store.claim(self.cycle)
        self.store.complete_success(self.cycle, 1, success_document())


class AnalysisSchedulerTests(unittest.TestCase):
    """Verifies the single-worker ownership and persisted restart behavior."""

    def setUp(self):
        """Creates isolated state and a controllable analyzer."""

        self.workspace = StateWorkspace()
        self.factory = ControlledOrchestratorFactory(self.workspace.root / "analysis")
        self.scheduler = AnalysisScheduler(self.workspace.store, self.factory)

    def tearDown(self):
        """Releases any block and stops the worker before deleting SQLite."""

        self.factory.release.set()
        self.scheduler.shutdown(timeout=2)
        self.workspace.close()

    def test_multiple_run_requests_coalesce_to_first_and_latest_generation(self):
        """Runs at most one old snapshot plus one latest snapshot for a burst."""

        self.factory.block_first = True
        self.scheduler.start()
        cycle = self.workspace.cycle_ids[0]
        self.scheduler.request_analysis(cycle, "run_uploaded", str(uuid.uuid4()))
        self.assertTrue(self.factory.started.wait(1))
        for _ in range(3):
            self.scheduler.request_analysis(cycle, "run_uploaded", str(uuid.uuid4()))
        self.factory.release.set()
        state = wait_for_status(self.workspace.store, cycle, "succeeded")
        self.assertEqual((state["requested_generation"], state["attempt_count"]), (4, 2))

    def test_same_cycle_has_at_most_one_running_analysis(self):
        """Never overlaps two attempts for one Cycle."""

        self._run_two_cycle_burst(use_same_cycle=True)
        self.assertEqual(self.factory.max_active, 1)

    def test_different_cycles_are_serialized_by_global_worker(self):
        """Keeps all Cycles on the same one-worker execution owner."""

        self._run_two_cycle_burst(use_same_cycle=False)
        self.assertEqual(self.factory.max_active, 1)

    def test_success_persists_small_report_references(self):
        """Writes identities and projected counters without LLVM JSON."""

        self.scheduler.start()
        cycle = self.workspace.cycle_ids[0]
        self.scheduler.request_analysis(cycle, "manual_retry")
        state = wait_for_status(self.workspace.store, cycle, "succeeded")
        self.assertEqual(state["latest_success_summary"]["covered"], 6)
        self.assertEqual(state["latest_success_summary"]["covered_with_inherited_evidence"], 3)

    def test_analysis_failure_is_terminal_without_automatic_retry(self):
        """Runs one failed generation exactly once."""

        self.factory.fail_next = True
        self.scheduler.start()
        cycle = self.workspace.cycle_ids[0]
        self.scheduler.request_analysis(cycle, "manual_retry")
        state = wait_for_status(self.workspace.store, cycle, "failed")
        self.assertEqual((state["attempt_count"], len(self.factory.calls)), (1, 1))

    def test_new_request_after_failure_runs_again(self):
        """Allows new data to request one new attempt after a deterministic failure."""

        self.factory.fail_next = True
        self.scheduler.start()
        cycle = self.workspace.cycle_ids[0]
        self.scheduler.request_analysis(cycle, "manual_retry")
        wait_for_status(self.workspace.store, cycle, "failed")
        self.scheduler.request_analysis(cycle, "run_uploaded", str(uuid.uuid4()))
        state = wait_for_status(self.workspace.store, cycle, "succeeded")
        self.assertEqual(state["attempt_count"], 2)

    def test_start_loads_persisted_queued_work(self):
        """Executes a request persisted before the worker existed."""

        cycle = self.workspace.cycle_ids[0]
        self.workspace.store.request_analysis(cycle, "manual_retry")
        self.scheduler.start()
        self.assertEqual(wait_for_status(self.workspace.store, cycle, "succeeded")["attempt_count"], 1)

    def test_start_recovers_interrupted_running_work(self):
        """Moves a dead-process running row to queued and executes it once."""

        cycle = self.workspace.cycle_ids[0]
        self.workspace.store.request_analysis(cycle, "manual_retry")
        self.workspace.store.claim(cycle)
        self.scheduler.start()
        state = wait_for_status(self.workspace.store, cycle, "succeeded")
        self.assertEqual((state["recovery_count"], state["attempt_count"]), (1, 2))

    def test_shutdown_does_not_kill_inflight_publish(self):
        """Returns after a bounded wait while retaining running recovery evidence."""

        self.factory.block_first = True
        self.scheduler.start()
        cycle = self.workspace.cycle_ids[0]
        self.scheduler.request_analysis(cycle, "manual_retry")
        self.assertTrue(self.factory.started.wait(1))
        self.assertFalse(self.scheduler.shutdown(timeout=0.01))
        self.assertEqual(self.workspace.store.get_state(cycle)["status"], "running")
        self.factory.release.set()
        wait_for_status(self.workspace.store, cycle, "succeeded")

    def test_worker_and_request_threads_open_distinct_connections(self):
        """Exercises the store from both threads without sharing SQLite handles."""

        self.scheduler.start()
        cycle = self.workspace.cycle_ids[0]
        request_thread = threading.get_ident()
        self.scheduler.request_analysis(cycle, "manual_retry")
        wait_for_status(self.workspace.store, cycle, "succeeded")
        self.assertNotEqual(request_thread, self.scheduler._worker.ident)

    def test_pause_waits_for_current_analysis_and_defers_next_claim(self):
        """Lets maintenance wait safely while queued generations remain durable."""

        self.factory.block_first = True
        self.scheduler.start()
        first, second = self.workspace.cycle_ids
        self.scheduler.request_analysis(first, "manual_retry")
        self.assertTrue(self.factory.started.wait(1))
        paused = threading.Event()

        def pause():
            self.scheduler.pause_and_wait_idle()
            paused.set()

        waiter = threading.Thread(target=pause)
        waiter.start()
        self.scheduler.request_analysis(second, "manual_retry")
        self.assertFalse(paused.wait(0.05))
        self.factory.release.set()
        self.assertTrue(paused.wait(1))
        self.assertEqual(self.workspace.store.get_state(second)["status"], "queued")
        self.scheduler.resume()
        waiter.join(timeout=1)
        self.assertEqual(wait_for_status(self.workspace.store, second, "succeeded")["status"], "succeeded")

    def test_pause_wait_is_cancellable_before_analysis_finishes(self):
        """Releases the maintenance pause promptly when server shutdown is requested."""

        self.factory.block_first = True
        self.scheduler.start()
        cycle = self.workspace.cycle_ids[0]
        self.scheduler.request_analysis(cycle, "manual_retry")
        self.assertTrue(self.factory.started.wait(1))
        cancellation = threading.Event()
        outcome = []

        def pause():
            try:
                self.scheduler.pause_and_wait_idle(cancellation)
            except AnalysisSchedulerError as error:
                outcome.append(error.code)

        waiter = threading.Thread(target=pause)
        waiter.start()
        time.sleep(0.03)
        cancellation.set()
        waiter.join(timeout=0.5)
        self.assertFalse(waiter.is_alive())
        self.assertEqual(outcome, ["maintenance_wait_cancelled"])
        self.factory.release.set()
        wait_for_status(self.workspace.store, cycle, "succeeded")

    def _run_two_cycle_burst(self, use_same_cycle):
        """Queues a blocked first attempt followed by same/different Cycle work."""

        self.factory.block_first = True
        self.scheduler.start()
        first = self.workspace.cycle_ids[0]
        second = first if use_same_cycle else self.workspace.cycle_ids[1]
        self.scheduler.request_analysis(first, "manual_retry")
        self.assertTrue(self.factory.started.wait(1))
        self.scheduler.request_analysis(second, "manual_retry")
        self.factory.release.set()
        wait_for_status(self.workspace.store, first, "succeeded")
        wait_for_status(self.workspace.store, second, "succeeded")


class HTTPWorkspace:
    """Runs the real HTTP routing and persistence with a controlled analyzer."""

    def __init__(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.data = self.root / "data"
        self.database = self.root / "instance" / "coverage.sqlite3"
        self.repository_path = self.root / "ExampleApp"
        self.repository_path.mkdir()
        self.token = "automatic-analysis-test-token"
        self.logger = logging.getLogger("automatic_analysis_http_{}".format(id(self)))
        self.logger.handlers.clear()
        self.logger.addHandler(logging.NullHandler())
        repository = FakeRepository(self.repository_path)
        validator = FakeArtifactValidator()
        coverage_store = CoverageStore(
            self.database, self.data,
            FakeProjectRegistry(repository, bundle_ids=("com.example.coverage",)), validator
        )
        self.cycle_id = coverage_store.create_cycle(
            {
                "schema_version": 1,
                "project": "ExampleApp",
                "target_branch": "master",
                "base_sha": "1" * 40,
                "head_sha": "3" * 40,
            }
        )["id"]
        self.state_store = AnalysisStateStore(self.database)
        self.factory = ControlledOrchestratorFactory(self.root / "analysis")
        self.scheduler = AnalysisScheduler(self.state_store, self.factory)
        config = ProbeConfig(
            token=self.token,
            data_dir=self.data,
            max_file_bytes=1024,
            max_run_bytes=1024 * 1024,
            max_build_artifact_bytes=1024 * 1024,
            instance_id=str(uuid.uuid4()),
            logger=self.logger,
            coverage_store=coverage_store,
            analysis_state_store=self.state_store,
            analysis_scheduler=self.scheduler,
        )
        self.server = ProbeHTTPServer(("127.0.0.1", 0), config)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.build_id = str(uuid.uuid4())
        self.register_direct(self.build_id)

    def close(self):
        """Stops HTTP and releases blocked analysis before removing files."""

        self.factory.release.set()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.temporary.cleanup()

    def request(self, method, path, body=b"", headers=None):
        """Sends one local request and parses its JSON response."""

        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=2)
        connection.request(method, path, body=body, headers=headers or {})
        response = connection.getresponse()
        payload = json.loads(response.read().decode("utf-8"))
        connection.close()
        return response.status, payload

    def auth(self):
        """Returns the existing Bearer header without exposing it in responses."""

        return {"Authorization": "Bearer " + self.token}

    def upload_run(self, build_id=None, run_id=None, payload=b"profraw"):
        """Uploads one valid Run through the unchanged multipart protocol."""

        build_id = build_id or self.build_id
        run_id = run_id or str(uuid.uuid4())
        metadata = {
            "schema_version": 1,
            "run_id": run_id,
            "cycle_id": self.cycle_id,
            "build_id": build_id,
            "device_id": str(uuid.uuid4()),
            "process_id": 123,
            "launch_id": str(uuid.uuid4()),
            "captured_at": "2026-09-01T00:00:00Z",
        }
        fields = {
            "metadata": json.dumps(metadata, sort_keys=True, separators=(",", ":")),
            "sha256": hashlib.sha256(payload).hexdigest(),
        }
        body, content_type = multipart(fields, {"profile": (run_id + ".profraw", payload)})
        headers = {**self.auth(), "Content-Type": content_type, "Content-Length": str(len(body))}
        return self.request("POST", "/api/v1/runs", body, headers), metadata

    def register_direct(self, build_id):
        """Registers one ready Build without exercising the HTTP trigger."""

        manifest, files = build_fixture(self.cycle_id, build_id)
        streams = {name: (filename, io.BytesIO(payload)) for name, (filename, payload) in files.items()}
        return self.server.config.coverage_store.register_build(manifest, streams, 1024 * 1024)

    def register_http(self, build_id):
        """Registers one Build through HTTP so pending promotion requests analysis."""

        manifest, files = build_fixture(self.cycle_id, build_id)
        body, content_type = multipart(
            {"manifest": json.dumps(manifest, sort_keys=True, separators=(",", ":"))}, files
        )
        return self.request(
            "POST",
            "/api/v1/builds",
            body,
            {**self.auth(), "Content-Type": content_type, "Content-Length": str(len(body))},
        )


class AutomaticAnalysisHTTPTests(unittest.TestCase):
    """Verifies trigger filters, response decoupling, and authenticated state APIs."""

    def setUp(self):
        """Starts one isolated automatic-analysis HTTP server."""

        self.workspace = HTTPWorkspace()

    def tearDown(self):
        """Stops the scheduler/server and removes fixtures."""

        self.workspace.close()

    def test_valid_registered_run_requests_analysis(self):
        """Queues analysis only after the durable uploaded/analyzable Run exists."""

        (status, payload), metadata = self.workspace.upload_run()
        self.assertEqual(status, 201)
        state = wait_for_generation(self.workspace.state_store, self.workspace.cycle_id, 1)
        self.assertEqual(state["latest_run_id"], metadata["run_id"])

    def test_run_response_does_not_wait_for_blocked_analysis(self):
        """Returns HTTP success while the worker is still blocked in orchestration."""

        self.workspace.factory.block_first = True
        started = time.monotonic()
        (status, _), _ = self.workspace.upload_run()
        elapsed = time.monotonic() - started
        self.assertEqual(status, 201)
        self.assertLess(elapsed, 1.0)
        self.assertTrue(self.workspace.factory.started.wait(1))
        self.assertEqual(self.workspace.state_store.get_state(self.workspace.cycle_id)["status"], "running")

    def test_runtime_lock_is_held_until_blocked_analysis_worker_stops(self):
        """Cancels bounded analysis before the owning runtime lock can be released."""

        self.workspace.factory.block_first = True
        self.workspace.factory.cancel_delay = 0.1
        self.workspace.scheduler.cancel_active_analysis = self.workspace.factory.cancel_active
        self.workspace.upload_run()
        self.assertTrue(self.workspace.factory.started.wait(1))
        lock_path = self.workspace.root / "state" / "server.lock"
        owner = RuntimeProcessLock(lock_path)
        owner.acquire()
        closed = threading.Event()
        self.workspace.server.shutdown()
        self.workspace.thread.join(timeout=1)

        def close_and_release():
            self.workspace.server.server_close()
            owner.release()
            closed.set()

        closer = threading.Thread(target=close_and_release)
        closer.start()
        time.sleep(0.03)
        self.assertFalse(closed.is_set())
        contender = RuntimeProcessLock(lock_path)
        with self.assertRaises(RuntimeLockError):
            contender.acquire()
        self.assertTrue(closed.wait(1))
        closer.join(timeout=1)
        self.assertEqual(
            self.workspace.state_store.get_state(self.workspace.cycle_id)["status"],
            "running",
        )
        contender.acquire()
        contender.release()

    def test_invalid_run_upload_does_not_trigger(self):
        """Leaves analysis idle when profraw SHA validation fails."""

        (status, _), metadata = self.workspace.upload_run(payload=b"")
        self.assertNotEqual(status, 201)
        self.assertEqual(self.workspace.state_store.get_state(self.workspace.cycle_id)["status"], "idle")

    def test_pending_build_run_does_not_trigger(self):
        """Accepts unknown-Build storage without claiming it analyzable."""

        (status, payload), _ = self.workspace.upload_run(build_id=str(uuid.uuid4()))
        self.assertEqual((status, payload["run_state"]), (202, "pending_build"))
        self.assertEqual(self.workspace.state_store.get_state(self.workspace.cycle_id)["status"], "idle")

    def test_identical_run_replay_does_not_trigger(self):
        """Keeps generation unchanged for a complete idempotent replay."""

        run_id = str(uuid.uuid4())
        first, metadata = self.workspace.upload_run(run_id=run_id)
        wait_for_generation(self.workspace.state_store, self.workspace.cycle_id, 1)
        payload = b"profraw"
        fields = {
            "metadata": json.dumps(metadata, sort_keys=True, separators=(",", ":")),
            "sha256": hashlib.sha256(payload).hexdigest(),
        }
        body, content_type = multipart(fields, {"profile": (run_id + ".profraw", payload)})
        status, response = self.workspace.request(
            "POST", "/api/v1/runs", body,
            {**self.workspace.auth(), "Content-Type": content_type, "Content-Length": str(len(body))},
        )
        self.assertEqual((status, response["idempotent_replay"]), (200, True))
        self.assertEqual(self.workspace.state_store.get_state(self.workspace.cycle_id)["requested_generation"], 1)

    def test_build_registration_with_pending_promotion_triggers(self):
        """Requests analysis only when a new Build actually promotes pending Runs."""

        build_id = str(uuid.uuid4())
        self.assertEqual(self.workspace.upload_run(build_id=build_id)[0][0], 202)
        status, payload = self.workspace.register_http(build_id)
        self.assertEqual((status, payload["pending_runs_promoted"]), (201, 1))
        self.assertEqual(wait_for_generation(self.workspace.state_store, self.workspace.cycle_id, 1)["last_trigger"], "pending_runs_promoted")

    def test_idempotent_build_replay_does_not_trigger(self):
        """Does not reuse historical promoted-count metadata as a fresh trigger."""

        build_id = str(uuid.uuid4())
        status, _ = self.workspace.register_http(build_id)
        self.assertEqual(status, 201)
        status, payload = self.workspace.register_http(build_id)
        self.assertEqual((status, payload["idempotent_replay"]), (200, True))
        self.assertEqual(self.workspace.state_store.get_state(self.workspace.cycle_id)["status"], "idle")

    def test_historical_build_run_also_triggers_current_cycle(self):
        """Does not restrict trigger eligibility to the Cycle head Build."""

        historical = str(uuid.uuid4())
        self.workspace.register_direct(historical)
        self.assertEqual(self.workspace.upload_run(build_id=historical)[0][0], 201)
        self.assertEqual(wait_for_generation(self.workspace.state_store, self.workspace.cycle_id, 1)["last_trigger"], "run_uploaded")

    def test_get_idle_and_succeeded_shapes(self):
        """Returns explicit nulls when idle and a bounded latest-success projection later."""

        path = "/api/v1/cycles/{}/analysis".format(self.workspace.cycle_id)
        status, idle = self.workspace.request("GET", path, headers=self.workspace.auth())
        self.assertEqual((status, idle["status"], idle["latest_success"]), (200, "idle", None))
        self.workspace.upload_run()
        wait_for_status(self.workspace.state_store, self.workspace.cycle_id, "succeeded")
        status, succeeded = self.workspace.request("GET", path, headers=self.workspace.auth())
        self.assertEqual(status, 200)
        self.assertEqual(succeeded["latest_success"]["summary"]["covered"], 6)
        self.assertNotIn("output_directory", json.dumps(succeeded))

    def test_get_queued_running_and_failed_retain_latest_success(self):
        """Keeps the last report visible through all non-idle scheduler states."""

        self.workspace.upload_run()
        wait_for_status(self.workspace.state_store, self.workspace.cycle_id, "succeeded")
        base = "/api/v1/cycles/{}/analysis".format(self.workspace.cycle_id)

        self.workspace.scheduler.shutdown(timeout=2)
        self.workspace.state_store.request_analysis(self.workspace.cycle_id, "manual_retry")
        queued = self.workspace.request("GET", base, headers=self.workspace.auth())[1]
        self.assertEqual(queued["status"], "queued")
        self.assertIsNotNone(queued["latest_success"])

        replacement = AnalysisScheduler(self.workspace.state_store, self.workspace.factory)
        self.workspace.factory.block_next = True
        replacement.start()
        wait_for_status(self.workspace.state_store, self.workspace.cycle_id, "running")
        running = self.workspace.request("GET", base, headers=self.workspace.auth())[1]
        self.assertEqual(running["status"], "running")
        self.assertIsNotNone(running["latest_success"])
        self.workspace.factory.release.set()
        wait_for_status(self.workspace.state_store, self.workspace.cycle_id, "succeeded")
        replacement.shutdown(timeout=2)

        self.workspace.state_store.request_analysis(self.workspace.cycle_id, "manual_retry")
        generation = self.workspace.state_store.claim(self.workspace.cycle_id)
        self.workspace.state_store.complete_failure(
            self.workspace.cycle_id, generation, "single_build", "fixture_failed", "failed"
        )
        failed = self.workspace.request("GET", base, headers=self.workspace.auth())[1]
        self.assertEqual(failed["status"], "failed")
        self.assertIsNotNone(failed["latest_success"])

    def test_get_and_retry_require_token(self):
        """Uses the existing Bearer boundary for both analysis endpoints."""

        base = "/api/v1/cycles/{}/analysis".format(self.workspace.cycle_id)
        self.assertEqual(self.workspace.request("GET", base)[0], 401)
        self.assertEqual(self.workspace.request("POST", base + "/retry")[0], 401)

    def test_retry_queues_and_coalesces_without_concurrency(self):
        """Accepts manual retry while running and schedules only the latest follow-up."""

        self.workspace.factory.block_first = True
        path = "/api/v1/cycles/{}/analysis/retry".format(self.workspace.cycle_id)
        self.assertEqual(self.workspace.request("POST", path, headers=self.workspace.auth())[0], 202)
        self.assertTrue(self.workspace.factory.started.wait(1))
        self.assertEqual(self.workspace.request("POST", path, headers=self.workspace.auth())[0], 202)
        self.workspace.factory.release.set()
        state = wait_for_status(self.workspace.state_store, self.workspace.cycle_id, "succeeded")
        self.assertEqual((state["requested_generation"], state["attempt_count"], self.workspace.factory.max_active), (2, 2, 1))

    def test_unknown_cycle_is_rejected_by_get_and_retry(self):
        """Returns 404 without creating orphan state."""

        cycle = str(uuid.uuid4())
        base = "/api/v1/cycles/{}/analysis".format(cycle)
        self.assertEqual(self.workspace.request("GET", base, headers=self.workspace.auth())[0], 404)
        self.assertEqual(self.workspace.request("POST", base + "/retry", headers=self.workspace.auth())[0], 404)

    def test_retry_rejects_body_and_caller_controlled_paths(self):
        """Accepts no JSON capable of overriding Build, Run, Git, command, or path inputs."""

        path = "/api/v1/cycles/{}/analysis/retry".format(self.workspace.cycle_id)
        body = json.dumps({"repository": "/tmp/evil", "build_id": str(uuid.uuid4())}).encode()
        status, payload = self.workspace.request(
            "POST", path, body, {**self.workspace.auth(), "Content-Length": str(len(body)), "Content-Type": "application/json"}
        )
        self.assertEqual((status, payload["error"]), (400, "request_body_not_allowed"))

    def test_failure_keeps_uploaded_run_and_latest_success(self):
        """Separates analysis failure from durable Run success and prior report state."""

        self.workspace.upload_run()
        wait_for_status(self.workspace.state_store, self.workspace.cycle_id, "succeeded")
        self.workspace.factory.fail_next = True
        path = "/api/v1/cycles/{}/analysis/retry".format(self.workspace.cycle_id)
        self.workspace.request("POST", path, headers=self.workspace.auth())
        state = wait_for_status(self.workspace.state_store, self.workspace.cycle_id, "failed")
        self.assertEqual(state["latest_success_generation"], 1)
        with sqlite3.connect(str(self.workspace.database)) as connection:
            self.assertEqual(connection.execute("SELECT state FROM runs").fetchone()[0], "uploaded")

    def test_error_api_does_not_expose_token(self):
        """Returns bounded failure fields without credentials or internal commands."""

        self.workspace.factory.fail_next = True
        path = "/api/v1/cycles/{}/analysis/retry".format(self.workspace.cycle_id)
        self.workspace.request("POST", path, headers=self.workspace.auth())
        wait_for_status(self.workspace.state_store, self.workspace.cycle_id, "failed")
        payload = self.workspace.request(
            "GET", "/api/v1/cycles/{}/analysis".format(self.workspace.cycle_id), headers=self.workspace.auth()
        )[1]
        serialized = json.dumps(payload)
        self.assertNotIn(self.workspace.token, serialized)
        self.assertEqual(payload["error"]["code"], "fixture_analysis_failed")


def success_document():
    """Returns one valid small state-store success payload."""

    return {
        "orchestration_input_sha256": "a" * 64,
        "cycle_input_set_sha256": "b" * 64,
        "current_build_id": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
        "output_directory": "/fixed/analysis/result",
        "summary": {"covered": 6, "uncovered": 4},
    }


def wait_for_status(store, cycle_id, expected, timeout=3):
    """Polls persisted state for one deterministic test transition."""

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        state = store.get_state(cycle_id)
        if state["status"] == expected:
            return state
        time.sleep(0.01)
    raise AssertionError("analysis status did not become " + expected)


def wait_for_generation(store, cycle_id, minimum, timeout=3):
    """Waits until a trigger transaction has committed the requested generation."""

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        state = store.get_state(cycle_id)
        if int(state["requested_generation"]) >= minimum:
            return state
        time.sleep(0.01)
    raise AssertionError("requested generation did not advance")


def build_fixture(cycle_id, build_id):
    """Creates one valid minimal Build manifest and multipart artifact set."""

    main = b"main executable"
    dsym = b"dsym zip"
    uuids = ["11111111-1111-4111-8111-111111111111"]
    manifest = {
        "schema_version": 1,
        "build_id": build_id,
        "cycle_id": cycle_id,
        "git_sha": "3" * 40,
        "target_sha_at_build": "2" * 40,
        "merge_base_sha": "1" * 40,
        "coverage_binary": "main_executable",
        "bundle_id": "com.example.coverage",
        "app_version": "1.0",
        "bundle_version": "1",
        "xcode_version": "26.3",
        "swift_version": "6",
        "macho_uuids": {"main_executable": uuids, "dsym": uuids},
        "artifacts": {
            "main_executable": {
                "filename": "ExampleApp",
                "size": len(main),
                "sha256": hashlib.sha256(main).hexdigest(),
            },
            "dsym": {
                "filename": "ExampleApp.app.dSYM.zip",
                "archive_format": "zip",
                "size": len(dsym),
                "sha256": hashlib.sha256(dsym).hexdigest(),
            },
        },
    }
    return manifest, {
        "main_executable": ("ExampleApp", main),
        "dsym": ("ExampleApp.app.dSYM.zip", dsym),
    }


def multipart(text_fields, file_fields):
    """Encodes a deterministic multipart body for the real request parser."""

    boundary = "ios-coverage-test-automatic-analysis-boundary"
    chunks = []
    for name, value in text_fields.items():
        chunks.append(
            ("--{}\r\nContent-Disposition: form-data; name=\"{}\"\r\n\r\n{}\r\n".format(boundary, name, value)).encode()
        )
    for name, (filename, payload) in file_fields.items():
        chunks.append(
            ("--{}\r\nContent-Disposition: form-data; name=\"{}\"; filename=\"{}\"\r\nContent-Type: application/octet-stream\r\n\r\n".format(boundary, name, filename)).encode()
            + payload
            + b"\r\n"
        )
    chunks.append(("--{}--\r\n".format(boundary)).encode())
    return b"".join(chunks), "multipart/form-data; boundary=" + boundary


if __name__ == "__main__":
    unittest.main()
