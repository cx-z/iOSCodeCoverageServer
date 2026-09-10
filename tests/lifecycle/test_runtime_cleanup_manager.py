"""Tests for startup, periodic, and operator-requested online cleanup."""

import http.client
import json
import logging
import socket
import subprocess
import tempfile
import threading
import time
import unittest
import uuid
from contextlib import contextmanager
from pathlib import Path

from ioscoverage.analysis.scheduler import AnalysisSchedulerError
from ioscoverage.lifecycle.manager import (
    MAX_SKIPPED_STATUS_ITEMS,
    RuntimeCleanupManager,
)
from ioscoverage.lifecycle.runtime_lock import RuntimeProcessLock
from ioscoverage.runtime import RuntimePaths
from ioscoverage.server.bootstrap import create_server
from ioscoverage.server.config import ProbeConfig
from ioscoverage.server.http.server import ProbeHTTPServer
from ioscoverage.storage.coverage_store import (
    CoverageStore,
    CoverageStoreMaintenanceCancelled,
)
from tests.helpers import project_registry


class FakeScheduler:
    """Records the pause barrier used by the cleanup manager."""

    def __init__(self):
        self.paused = False

    def pause_and_wait_idle(self, cancellation_event=None):
        self.paused = True

    def resume(self):
        self.paused = False


class FakeStore:
    """Exposes one observable maintenance guard."""

    def __init__(self):
        self.guarded = False

    @contextmanager
    def maintenance_guard(self, cancellation_event=None):
        self.guarded = True
        try:
            yield
        finally:
            self.guarded = False


class ControlledCleaner:
    """Emulates a coordinated deletion and optionally blocks one attempt."""

    def __init__(self, block_first=False):
        self.block_first = block_first
        self.started = threading.Event()
        self.release = threading.Event()
        self.calls = 0

    def run_online(self, process_lock, scheduler, store, cancellation_event=None):
        self.calls += 1
        if not process_lock.is_acquired:
            raise AssertionError("runtime lock was not owned")
        scheduler.pause_and_wait_idle(cancellation_event)
        try:
            with store.maintenance_guard(cancellation_event):
                if not scheduler.paused or not store.guarded:
                    raise AssertionError("cleanup coordination was not active")
                self.started.set()
                if self.block_first and self.calls == 1:
                    self.release.wait(2)
                return {
                    "dry_run": False,
                    "planned_deletion": {"run_profraw": {"items": 1, "bytes": 10}},
                    "actual_deletion": {"run_profraw": {"items": 1, "bytes": 10}},
                    "planned_release_bytes": 10,
                    "released_bytes": 10,
                    "skipped_count": 0,
                    "skipped": [],
                }
        finally:
            scheduler.resume()


class RuntimeCleanupManagerTests(unittest.TestCase):
    """Verifies timer reset, coalescing, and the nonblocking manual boundary."""

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="ios-coverage-cleanup-manager-")
        self.lock = RuntimeProcessLock(Path(self.temporary.name) / "state" / "server.lock")
        self.lock.acquire()
        self.scheduler = FakeScheduler()
        self.store = FakeStore()
        self.logger = logging.getLogger("cleanup_manager_test_{}".format(id(self)))
        self.logger.handlers.clear()
        self.logger.addHandler(logging.NullHandler())
        self.managers = []

    def tearDown(self):
        for manager in self.managers:
            manager.shutdown()
        self.lock.release()
        self.temporary.cleanup()

    def manager(self, cleaner, interval=3600):
        manager = RuntimeCleanupManager(
            cleaner, self.lock, self.scheduler, self.store, self.logger,
            interval_seconds=interval,
        )
        self.managers.append(manager)
        return manager

    def test_startup_cleanup_is_synchronous_and_timer_starts_afterward(self):
        cleaner = ControlledCleaner()
        manager = self.manager(cleaner, interval=3600)
        state = manager.run_startup_cleanup()
        self.assertEqual((cleaner.calls, state["status"], state["last_trigger"]), (1, "succeeded", "startup"))
        self.assertEqual(state["last_result"]["released_bytes"], 10)
        manager.start()
        time.sleep(0.03)
        self.assertEqual(cleaner.calls, 1)

    def test_manual_request_returns_before_blocked_cleanup_completes(self):
        cleaner = ControlledCleaner(block_first=True)
        manager = self.manager(cleaner)
        manager.start()
        started = time.monotonic()
        state = manager.request_cleanup()
        self.assertLess(time.monotonic() - started, 0.2)
        self.assertIn(state["status"], ("queued", "running"))
        self.assertTrue(cleaner.started.wait(1))
        cleaner.release.set()
        self.assertTrue(wait_until(lambda: manager.get_state()["status"] == "succeeded"))

    def test_requests_during_cleanup_coalesce_into_one_followup(self):
        cleaner = ControlledCleaner(block_first=True)
        manager = self.manager(cleaner)
        manager.start()
        manager.request_cleanup()
        self.assertTrue(cleaner.started.wait(1))
        manager.request_cleanup()
        manager.request_cleanup()
        cleaner.release.set()
        self.assertTrue(wait_until(lambda: cleaner.calls == 2))
        self.assertTrue(wait_until(lambda: manager.get_state()["status"] == "succeeded"))
        self.assertEqual(cleaner.calls, 2)

    def test_periodic_cleanup_runs_after_one_interval(self):
        cleaner = ControlledCleaner()
        manager = self.manager(cleaner, interval=0.05)
        manager.start()
        self.assertTrue(wait_until(lambda: cleaner.calls >= 1))
        self.assertEqual(manager.get_state()["last_trigger"], "periodic")

    def test_shutdown_cancels_cleanup_waiting_for_analysis(self):
        """Returns promptly when deletion has not begun and analysis remains active."""

        class WaitingScheduler(FakeScheduler):
            def __init__(inner_self):
                super().__init__()
                inner_self.waiting = threading.Event()

            def pause_and_wait_idle(inner_self, cancellation_event=None):
                inner_self.paused = True
                inner_self.waiting.set()
                while cancellation_event is None or not cancellation_event.wait(0.01):
                    pass
                inner_self.paused = False
                raise AnalysisSchedulerError(
                    "maintenance_wait_cancelled", "maintenance wait was cancelled"
                )

        scheduler = WaitingScheduler()
        manager = RuntimeCleanupManager(
            ControlledCleaner(), self.lock, scheduler, self.store, self.logger,
            interval_seconds=3600,
        )
        self.managers.append(manager)
        manager.start()
        manager.request_cleanup()
        self.assertTrue(scheduler.waiting.wait(1))
        started = time.monotonic()
        manager.shutdown()
        self.assertLess(time.monotonic() - started, 0.5)
        self.assertFalse(manager._worker.is_alive())

    def test_maintenance_guard_wait_can_be_cancelled_before_acquisition(self):
        """Does not enter deletion when shutdown occurs behind an active mutation."""

        store = CoverageStore.__new__(CoverageStore)
        store._mutation_lock = threading.RLock()
        cancellation = threading.Event()
        finished = threading.Event()
        outcome = []

        def wait_for_guard():
            try:
                with store.maintenance_guard(cancellation):
                    outcome.append("acquired")
            except CoverageStoreMaintenanceCancelled:
                outcome.append("cancelled")
            finally:
                finished.set()

        with store._mutation_lock:
            waiter = threading.Thread(target=wait_for_guard)
            waiter.start()
            time.sleep(0.03)
            cancellation.set()
            self.assertTrue(finished.wait(0.5))
        waiter.join(timeout=1)
        self.assertEqual(outcome, ["cancelled"])

    def test_running_trigger_is_not_replaced_by_queued_manual_trigger(self):
        """Keeps the active trigger accurate while exposing one pending follow-up."""

        cleaner = ControlledCleaner(block_first=True)
        manager = self.manager(cleaner, interval=0.01)
        manager.start()
        self.assertTrue(cleaner.started.wait(1))
        self.assertEqual(manager.get_state()["last_trigger"], "periodic")
        state = manager.request_cleanup()
        self.assertEqual(state["last_trigger"], "periodic")
        self.assertEqual(state["pending_trigger"], "manual")
        cleaner.release.set()
        self.assertTrue(wait_until(lambda: cleaner.calls >= 2))

    def test_online_status_caps_skipped_details_and_copies_nested_state(self):
        """Bounds skipped diagnostics and does not expose manager-owned containers."""

        skipped = [
            {"cycle_id": str(uuid.uuid4()), "reason": "report_not_available"}
            for _ in range(MAX_SKIPPED_STATUS_ITEMS + 5)
        ]
        result = {
            "dry_run": False,
            "planned_deletion": {"run_profraw": {"items": 0, "bytes": 0}},
            "actual_deletion": {"run_profraw": {"items": 0, "bytes": 0}},
            "planned_release_bytes": 0,
            "released_bytes": 0,
            "skipped_count": len(skipped),
            "skipped": skipped,
        }
        projection = RuntimeCleanupManager._result_projection(result)
        self.assertEqual(len(projection["skipped"]), MAX_SKIPPED_STATUS_ITEMS)
        cleaner = ControlledCleaner()
        manager = self.manager(cleaner)
        manager._last_result = projection
        first = manager.get_state()
        first["last_result"]["planned_deletion"]["run_profraw"]["items"] = 99
        self.assertEqual(
            manager.get_state()["last_result"]["planned_deletion"]["run_profraw"]["items"],
            0,
        )


class FakeHTTPManager:
    """Supplies a bounded cleanup route fixture."""

    def __init__(self):
        self.requests = 0

    def start(self):
        pass

    def run_startup_cleanup(self):
        return self._state("succeeded")

    def shutdown(self):
        pass

    def get_state(self):
        return self._state("succeeded")

    def request_cleanup(self):
        self.requests += 1
        return self._state("queued")

    def _state(self, status):
        return {
            "status": status,
            "requested_count": self.requests,
            "completed_count": 0,
            "last_trigger": "manual" if self.requests else "startup",
            "pending_trigger": "manual" if status == "queued" else None,
            "last_started_at": None,
            "last_completed_at": None,
            "interval_seconds": 86400,
            "last_result": None,
            "error": None,
        }


class RuntimeCleanupHTTPTests(unittest.TestCase):
    """Verifies operator-only status and asynchronous trigger routes."""

    def setUp(self):
        self.manager = FakeHTTPManager()
        logger = logging.getLogger("cleanup_http_test_{}".format(id(self)))
        logger.handlers.clear()
        logger.addHandler(logging.NullHandler())
        class StateStore:
            def recover_interrupted(inner_self):
                return []

        config = ProbeConfig(
            token="operator-token",
            run_upload_token="run-token",
            data_dir=Path(tempfile.gettempdir()),
            max_file_bytes=1024,
            max_run_bytes=1024,
            max_build_artifact_bytes=1024,
            instance_id=str(uuid.uuid4()),
            logger=logger,
            coverage_store=object(),
            analysis_state_store=StateStore(),
            analysis_scheduler=None,
            runtime_cleanup_manager=self.manager,
        )
        self.server = ProbeHTTPServer(("127.0.0.1", 0), config)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def request(self, method, body=b"", token="operator-token"):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=2)
        headers = {"Authorization": "Bearer " + token}
        if body:
            headers["Content-Length"] = str(len(body))
        connection.request(method, "/api/v1/maintenance/cleanup", body=body, headers=headers)
        response = connection.getresponse()
        payload = json.loads(response.read().decode("utf-8"))
        connection.close()
        return response.status, payload

    def test_operator_can_read_and_request_cleanup(self):
        self.assertEqual(self.request("GET")[0], 200)
        status, payload = self.request("POST")
        self.assertEqual((status, payload["status"], self.manager.requests), (202, "queued", 1))

    def test_run_token_is_forbidden_and_body_is_rejected(self):
        self.assertEqual(self.request("POST", token="run-token")[0], 403)
        status, payload = self.request("POST", body=b"{}")
        self.assertEqual((status, payload["error"]), (400, "request_body_not_allowed"))

    def test_invalid_content_length_and_transfer_encoding_are_rejected(self):
        """Rejects ambiguous framing instead of treating it as an absent body."""

        for framing in (
            "Content-Length: not-a-number\r\n",
            "Transfer-Encoding: chunked\r\n",
        ):
            request = (
                "POST /api/v1/maintenance/cleanup HTTP/1.1\r\n"
                "Host: 127.0.0.1\r\n"
                "Authorization: Bearer operator-token\r\n"
                + framing
                + "Connection: close\r\n\r\n"
            ).encode("ascii")
            with socket.create_connection(
                ("127.0.0.1", self.server.server_port), timeout=2
            ) as connection:
                connection.sendall(request)
                response = connection.recv(4096)
            self.assertIn(b" 400 ", response)
        self.assertEqual(self.manager.requests, 0)


class RuntimeCleanupBootstrapTests(unittest.TestCase):
    """Verifies production assembly runs cleanup before exposing the server."""

    def test_enabled_server_runs_startup_cleanup_and_starts_timer(self):
        with tempfile.TemporaryDirectory(prefix="ios-coverage-cleanup-bootstrap-") as value:
            root = Path(value)
            repository = root / "ExampleApp"
            subprocess.run(
                ["git", "init", str(repository)],
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            paths = RuntimePaths.from_root(root / "runtime")
            lock = RuntimeProcessLock(paths.server_lock)
            lock.acquire()
            server = None
            logger = logging.getLogger("cleanup_bootstrap_test_{}".format(id(self)))
            logger.handlers.clear()
            logger.addHandler(logging.NullHandler())
            try:
                server = create_server(
                    "127.0.0.1",
                    0,
                    "operator-token",
                    paths,
                    project_registry(repository),
                    logger=logger,
                    analysis_orchestrator_factory=lambda: object(),
                    runtime_process_lock=lock,
                    enable_runtime_cleanup=True,
                    cleanup_interval_seconds=3600,
                )
                state = server.config.runtime_cleanup_manager.get_state()
                self.assertEqual(
                    (state["status"], state["last_trigger"], state["completed_count"]),
                    ("succeeded", "startup", 1),
                )
            finally:
                if server is not None:
                    server.server_close()
                lock.release()


def wait_until(predicate, timeout=2.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False
