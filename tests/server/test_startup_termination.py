"""Real isolated bootstrap signals: cleanup/teardown remain protected by the runtime lease."""

import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import Mock
from types import SimpleNamespace

from ioscoverage.lifecycle.runtime_lock import RuntimeProcessLock, RuntimeLockError
from ioscoverage.runtime import RuntimePaths
from ioscoverage.server.http.server import ProbeHTTPServer
from tests.helpers import project_registry


CHILD = r'''
import json, sys, time
from pathlib import Path
from ioscoverage.runtime import RuntimePaths
from ioscoverage.server import bootstrap
from ioscoverage.server.deployment import ServerDeploymentConfiguration
from ioscoverage.lifecycle.cleanup import RuntimeDataCleaner
from ioscoverage.lifecycle.manager import RuntimeCleanupManager
from ioscoverage.server.http.server import ProbeHTTPServer

root, mode, port = Path(sys.argv[1]), sys.argv[2], sys.argv[3]
paths = RuntimePaths.from_root(root)
bootstrap.ServerDeploymentConfiguration.load = lambda: ServerDeploymentConfiguration(paths)
bootstrap.GitLabMRClient = lambda *args: None
sys.argv = ['server.py', '--host', '127.0.0.1', '--port', port]

def barrier(name):
    (root / name).touch()
    deadline = time.monotonic() + 15
    while not (root / (name + '-release')).exists():
        if time.monotonic() > deadline: raise RuntimeError('fixture barrier timeout')
        time.sleep(0.01)

def cleanup(self, lock, scheduler, store, cancellation_event=None):
    scheduler.pause_and_wait_idle(cancellation_event)
    try:
        with store.maintenance_guard(cancellation_event):
            assert lock.is_acquired
            if mode == 'startup': barrier('deleting')
            (root / 'deletion-completed').touch()
    finally:
        scheduler.resume()
    return dict(dry_run=False, planned_deletion={}, actual_deletion={},
                planned_release_bytes=0, released_bytes=0, skipped_count=0, skipped=[])

RuntimeDataCleaner.run_online = cleanup
original_serve = ProbeHTTPServer.serve_forever
original_close = ProbeHTTPServer.server_close
original_shutdown = RuntimeCleanupManager.shutdown

def serve(self, *args, **kwargs):
    (root / 'serving').touch()
    return original_serve(self, *args, **kwargs)

def close(self):
    original_close(self)
    (root / 'owners-closed').touch()

def shutdown(self):
    original_shutdown(self)
    if mode == 'closing': barrier('closing')

ProbeHTTPServer.serve_forever = serve
ProbeHTTPServer.server_close = close
RuntimeCleanupManager.shutdown = shutdown
bootstrap.main()
'''


class StartupTerminationTests(unittest.TestCase):
    """Only operate children and runtime directories created by these tests."""

    def wait_file(self, path, process):
        """Waits a bounded time for a child phase without consuming its diagnostic output."""
        deadline = time.monotonic() + 10
        while not path.exists():
            if process.poll() is not None:
                self.fail("isolated server exited before " + path.name)
            if time.monotonic() > deadline:
                self.fail("isolated server did not reach " + path.name)
            time.sleep(0.02)

    def assert_lock_owned(self, path):
        """Confirms termination intent cannot release ownership inside a protected boundary."""
        contender = RuntimeProcessLock(path)
        try:
            with self.assertRaises(RuntimeLockError):
                contender.acquire()
        finally:
            contender.release()

    def test_signals_during_startup_serving_and_shutdown(self):
        """TERM/INT during deletion or owner teardown defer; serve-loop termination exits cleanly."""
        for mode in ("startup", "serving", "closing"):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                paths = RuntimePaths.from_root(root)
                paths.prepare_directories()
                repository = root / "ExampleApp"
                subprocess.run(["git", "init", str(repository)], check=True, capture_output=True)
                paths.projects_file.write_text(json.dumps(project_registry(repository).to_document()))
                with socket.socket() as reservation:
                    reservation.bind(("127.0.0.1", 0))
                    port = reservation.getsockname()[1]
                with (root / "child-output").open("wb") as output:
                    process = subprocess.Popen([sys.executable, "-c", CHILD, str(root), mode, str(port)],
                                               stdout=output, stderr=output)
                    try:
                        if mode == "startup":
                            self.wait_file(root / "deleting", process)
                        else:
                            self.wait_file(root / "serving", process)
                        process.send_signal(signal.SIGTERM)
                        if mode != "serving":
                            phase = "deleting" if mode == "startup" else "closing"
                            self.wait_file(root / phase, process)
                            process.send_signal(signal.SIGTERM)
                            process.send_signal(signal.SIGINT)
                            time.sleep(0.1)
                            self.assertIsNone(process.poll())
                            self.assert_lock_owned(paths.server_lock)
                            (root / (phase + "-release")).touch()
                        self.assertEqual(process.wait(timeout=10), 0)
                        self.assertTrue((root / "deletion-completed").exists())
                        self.assertTrue((root / "owners-closed").exists())
                        with RuntimeProcessLock(paths.server_lock):
                            pass
                        events = [json.loads(line)["event"] for line in paths.server_log.read_text().splitlines()]
                        self.assertIn("server_stopped", events)
                        if mode == "startup":
                            self.assertNotIn("server_started", events)
                            self.assertFalse((root / "serving").exists())
                    finally:
                        for phase in ("deleting", "closing"):
                            (root / (phase + "-release")).touch()
                        if process.poll() is None:
                            process.send_signal(signal.SIGTERM)
                            process.wait(timeout=10)

    def test_partial_constructor_unwinds_all_owners(self):
        """BaseException during worker startup still closes other owners and the listener."""
        scheduler, manager, repository = Mock(), Mock(), Mock()
        scheduler.start.side_effect = KeyboardInterrupt
        scheduler.shutdown.return_value = True
        config = SimpleNamespace(analysis_scheduler=scheduler, runtime_cleanup_manager=manager,
                                 repository_sync_service=repository, analysis_state_store=Mock(), logger=Mock())
        with self.assertRaises(KeyboardInterrupt):
            ProbeHTTPServer(("127.0.0.1", 0), config)
        scheduler.shutdown.assert_called_once()
        manager.shutdown.assert_called_once()
        repository.shutdown.assert_called_once()

    def test_close_failure_still_attempts_other_owners(self):
        """One failed collaborator cannot skip analysis cancellation or silently release ownership."""
        config = SimpleNamespace(analysis_scheduler=Mock(), runtime_cleanup_manager=Mock(),
                                 repository_sync_service=Mock(), analysis_state_store=Mock(), logger=Mock())
        config.analysis_scheduler.shutdown.return_value = True
        server = ProbeHTTPServer(("127.0.0.1", 0), config)
        config.runtime_cleanup_manager.shutdown.side_effect = RuntimeError("cleanup failed")
        with self.assertRaisesRegex(RuntimeError, "cleanup failed"):
            server.server_close()
        config.analysis_scheduler.shutdown.assert_called_once()
        config.repository_sync_service.shutdown.assert_called_once()
        self.assertEqual(server.socket.fileno(), -1)
