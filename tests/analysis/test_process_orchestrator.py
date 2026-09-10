#!/usr/bin/env python3
"""Tests the bounded process boundary used by production background analysis."""

import os
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

from ioscoverage.analysis.orchestrator import CycleAnalysisOrchestrationError
from ioscoverage.analysis.process_orchestrator import ProcessCycleAnalysisOrchestrator
from ioscoverage.analysis.scheduler import AnalysisScheduler
from ioscoverage.lifecycle.runtime_lock import RuntimeLockError, RuntimeProcessLock
from ioscoverage.projects import ProjectRegistry
from ioscoverage.runtime import RuntimePaths
from ioscoverage.storage.analysis_state import AnalysisStateStore
from ioscoverage.storage.coverage_store import CoverageStore


class ProcessCycleAnalysisOrchestratorTests(unittest.TestCase):
    """Verifies result transport and confirmed shutdown of a stuck execution unit."""

    def setUp(self):
        """Creates harmless fixed paths for the private process adapter."""

        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)

    def tearDown(self):
        """Removes the isolated test directory."""

        self.temporary.cleanup()

    def test_successful_child_result_preserves_orchestration_contract(self):
        """Returns the same result shape expected by the existing scheduler."""

        payload = {
            "status": "complete",
            "result": {
                "orchestration_input_sha256": "a" * 64,
                "output_directory": str(self.root / "analysis"),
                "summary": {"current_build_id": "fixture"},
                "idempotent_replay": True,
            },
        }
        orchestrator = self._orchestrator(
            lambda _cycle: (
                sys.executable,
                "-c",
                "import json; print(json.dumps({!r}))".format(payload),
            )
        )
        result = orchestrator.analyze("cycle")
        self.assertEqual(result.orchestration_input_sha256, "a" * 64)
        self.assertTrue(result.idempotent_replay)

    def test_shutdown_kills_child_that_ignores_termination(self):
        """Escalates to process-group kill and returns without an infinite join."""

        ready = self.root / "worker-ready"
        command = (
            sys.executable,
            "-c",
            (
                "import pathlib,signal,time; "
                "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
                "pathlib.Path({!r}).write_text('ready'); time.sleep(60)"
            ).format(str(ready)),
        )
        orchestrator = self._orchestrator(lambda _cycle: command)
        errors = []

        def analyze():
            try:
                orchestrator.analyze("cycle")
            except CycleAnalysisOrchestrationError as error:
                errors.append(error.code)

        worker = threading.Thread(target=analyze)
        worker.start()
        deadline = time.monotonic() + 2
        while not ready.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertTrue(ready.exists())
        started = time.monotonic()
        orchestrator.cancel_active()
        worker.join(timeout=1)
        self.assertFalse(worker.is_alive())
        self.assertLess(time.monotonic() - started, 0.5)
        self.assertEqual(errors, ["analysis_cancelled"])

    def test_shutdown_before_spawn_prevents_new_execution(self):
        """Closes the spawn race after scheduler shutdown has begun."""

        called = []
        orchestrator = self._orchestrator(lambda cycle: called.append(cycle))
        orchestrator.cancel_active()
        with self.assertRaises(CycleAnalysisOrchestrationError) as context:
            orchestrator.analyze("cycle")
        self.assertEqual(context.exception.code, "analysis_cancelled")
        self.assertEqual(called, [])

    def test_analysis_lease_survives_parent_sigterm_and_sigkill(self):
        """Prevents runtime reacquisition while an orphaned analysis group remains."""

        source_root = Path(__file__).resolve().parents[2]
        for termination_signal in (signal.SIGTERM, signal.SIGKILL):
            with self.subTest(signal=termination_signal):
                case_root = self.root / ("parent-exit-" + str(termination_signal))
                case_root.mkdir()
                lock_path = case_root / "state" / "server.lock"
                group_id_file = case_root / "analysis-group-id"
                child_code = (
                    "import os,pathlib,time; "
                    "pathlib.Path({!r}).write_text(str(os.getpid())); time.sleep(60)"
                ).format(str(group_id_file))
                parent_code = (
                    "import pathlib,sys; "
                    "from ioscoverage.analysis.process_orchestrator import "
                    "ProcessCycleAnalysisOrchestrator; "
                    "from ioscoverage.lifecycle.runtime_lock import RuntimeProcessLock; "
                    "root=pathlib.Path(sys.argv[1]); "
                    "lock=RuntimeProcessLock(root/'state'/'server.lock'); lock.acquire(); "
                    "adapter=ProcessCycleAnalysisOrchestrator(root,"
                    "{'schema_version':1,'projects':[]},root,"
                    "command_factory=lambda _cycle:(sys.executable,'-c',sys.argv[2]),"
                    "runtime_lock_descriptor=lock.lease_descriptor); "
                    "adapter.analyze('cycle')"
                )
                parent = subprocess.Popen(
                    (sys.executable, "-c", parent_code, str(case_root), child_code),
                    cwd=str(source_root),
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    close_fds=True,
                )
                process_group_id = None
                contender = RuntimeProcessLock(lock_path)
                try:
                    deadline = time.monotonic() + 3
                    while not group_id_file.exists() and time.monotonic() < deadline:
                        time.sleep(0.01)
                    self.assertTrue(group_id_file.exists())
                    process_group_id = int(group_id_file.read_text(encoding="utf-8"))
                    os.kill(parent.pid, termination_signal)
                    parent.wait(timeout=2)
                    self.assertTrue(
                        ProcessCycleAnalysisOrchestrator._group_exists(process_group_id)
                    )
                    with self.assertRaises(RuntimeLockError):
                        contender.acquire()

                    os.killpg(process_group_id, signal.SIGKILL)
                    deadline = time.monotonic() + 2
                    while (
                        ProcessCycleAnalysisOrchestrator._group_exists(process_group_id)
                        and time.monotonic() < deadline
                    ):
                        time.sleep(0.01)
                    self.assertFalse(
                        ProcessCycleAnalysisOrchestrator._group_exists(process_group_id)
                    )
                    contender.acquire()
                    contender.release()
                finally:
                    if parent.poll() is None:
                        parent.kill()
                        parent.wait(timeout=2)
                    if (
                        process_group_id is not None
                        and ProcessCycleAnalysisOrchestrator._group_exists(
                            process_group_id
                        )
                    ):
                        os.killpg(process_group_id, signal.SIGKILL)
                    contender.release()

    def test_leader_failure_reaps_descendants_before_scheduler_is_idle(self):
        """Keeps cleanup blocked until an abnormal worker's process group is empty."""

        paths, registry, cycle_id = self._runtime_cycle()
        group_id_file = self.root / "process-group-id"
        grandchild = (
            "import signal,time; "
            "signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(60)"
        )
        leader = (
            "import os,pathlib,subprocess,sys,time; "
            "pathlib.Path({!r}).write_text(str(os.getpid())); "
            "subprocess.Popen([sys.executable,'-c',{!r}],stdin=subprocess.DEVNULL,"
            "stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL); "
            "time.sleep(0.1); raise SystemExit(7)"
        ).format(str(group_id_file), grandchild)
        orchestrator = ProcessCycleAnalysisOrchestrator(
            paths.root,
            registry.to_document(),
            self.root,
            command_factory=lambda _cycle: (sys.executable, "-c", leader),
            terminate_grace_seconds=0.05,
            kill_grace_seconds=0.5,
        )
        state_store = AnalysisStateStore(paths.database)
        scheduler = AnalysisScheduler(
            state_store,
            lambda: orchestrator,
            cancel_active_analysis=orchestrator.cancel_active,
        )
        process_group_id = None
        scheduler.start()
        try:
            scheduler.request_analysis(cycle_id, "manual_retry")
            deadline = time.monotonic() + 3
            while not group_id_file.exists() and time.monotonic() < deadline:
                time.sleep(0.01)
            process_group_id = int(group_id_file.read_text(encoding="utf-8"))
            self.assertTrue(orchestrator._group_exists(process_group_id))
            barrier_passed = threading.Event()

            def wait_for_idle():
                scheduler.pause_and_wait_idle()
                barrier_passed.set()

            barrier_thread = threading.Thread(target=wait_for_idle)
            barrier_thread.start()
            self.assertFalse(barrier_passed.wait(0.03))
            state = state_store.get_state(cycle_id)
            while state["status"] != "failed" and time.monotonic() < deadline:
                time.sleep(0.01)
                state = state_store.get_state(cycle_id)
            self.assertEqual(state["status"], "failed")
            self.assertEqual(state["error_code"], "analysis_worker_descendants_leaked")
            self.assertFalse(orchestrator._group_exists(process_group_id))
            self.assertIsNone(orchestrator._active)
            self.assertTrue(barrier_passed.wait(1))
            barrier_thread.join(timeout=1)
            scheduler.resume()
        finally:
            scheduler.shutdown(timeout=1)
            if process_group_id is not None and orchestrator._group_exists(process_group_id):
                os.killpg(process_group_id, signal.SIGKILL)

    def test_termination_failure_keeps_scheduler_and_cleanup_fail_closed(self):
        """Retains execution ownership until an unconfirmed process group is gone."""

        paths, registry, cycle_id = self._runtime_cycle()
        group_id_file = self.root / "unresolved-process-group-id"
        grandchild = (
            "import signal,time; "
            "signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(60)"
        )
        leader = (
            "import os,pathlib,subprocess,sys,time; "
            "pathlib.Path({!r}).write_text(str(os.getpid())); "
            "subprocess.Popen([sys.executable,'-c',{!r}],stdin=subprocess.DEVNULL,"
            "stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL); "
            "time.sleep(0.1); raise SystemExit(7)"
        ).format(str(group_id_file), grandchild)
        spawn_count = []

        def command_factory(cycle):
            spawn_count.append(cycle)
            return (sys.executable, "-c", leader)

        orchestrator = ProcessCycleAnalysisOrchestrator(
            paths.root,
            registry.to_document(),
            self.root,
            command_factory=command_factory,
            terminate_grace_seconds=0.05,
            kill_grace_seconds=0.5,
        )
        real_stop_process_group = orchestrator._stop_process_group
        termination_failure_observed = threading.Event()

        def fail_to_stop(_process):
            termination_failure_observed.set()
            raise RuntimeError("injected process-group termination failure")

        orchestrator._stop_process_group = fail_to_stop
        state_store = AnalysisStateStore(paths.database)
        scheduler = AnalysisScheduler(
            state_store,
            lambda: orchestrator,
            cancel_active_analysis=orchestrator.cancel_active,
            active_analysis_check=orchestrator.has_active_process_group,
        )
        process_group_id = None
        barrier_thread = None
        barrier_errors = []
        scheduler.start()
        try:
            scheduler.request_analysis(cycle_id, "manual_retry")
            deadline = time.monotonic() + 3
            while not group_id_file.exists() and time.monotonic() < deadline:
                time.sleep(0.01)
            process_group_id = int(group_id_file.read_text(encoding="utf-8"))
            self.assertTrue(termination_failure_observed.wait(2))
            state = state_store.get_state(cycle_id)
            self.assertEqual(state["status"], "running")
            self.assertTrue(orchestrator._group_exists(process_group_id))
            self.assertIsNotNone(orchestrator._active)
            self.assertEqual(scheduler.running_cycle, cycle_id)

            barrier_passed = threading.Event()

            def wait_for_idle():
                try:
                    scheduler.pause_and_wait_idle()
                    barrier_passed.set()
                except Exception as error:
                    barrier_errors.append(error)

            barrier_thread = threading.Thread(target=wait_for_idle)
            barrier_thread.start()
            self.assertFalse(barrier_passed.wait(0.1))

            with self.assertRaises(CycleAnalysisOrchestrationError) as context:
                orchestrator.analyze("another-cycle")
            self.assertEqual(context.exception.code, "analysis_worker_termination_failed")
            self.assertEqual(spawn_count, [cycle_id])

            orchestrator._stop_process_group = real_stop_process_group
            real_stop_process_group(orchestrator._active)
            self.assertTrue(barrier_passed.wait(1))
            barrier_thread.join(timeout=1)
            self.assertEqual(barrier_errors, [])
            state = state_store.get_state(cycle_id)
            self.assertEqual(state["status"], "failed")
            self.assertEqual(state["error_code"], "analysis_worker_termination_failed")
            self.assertIsNone(scheduler.running_cycle)
            self.assertFalse(orchestrator.has_active_process_group())
            scheduler.resume()
        finally:
            orchestrator._stop_process_group = real_stop_process_group
            if process_group_id is not None and orchestrator._group_exists(process_group_id):
                os.killpg(process_group_id, signal.SIGKILL)
            if barrier_thread is not None and barrier_thread.is_alive():
                scheduler.resume()
            scheduler.shutdown(timeout=1)
            if barrier_thread is not None:
                barrier_thread.join(timeout=1)

    def test_post_commit_failure_cannot_bypass_cleanup_safety_wait(self):
        """Guards cleanup before a failure-state commit can itself raise."""

        paths, _registry, cycle_id = self._runtime_cycle()
        analysis_started = threading.Event()
        unresolved_group = threading.Event()
        unresolved_group.set()

        class TerminationFailureOrchestrator:
            def analyze(self, _cycle_id):
                analysis_started.set()
                raise CycleAnalysisOrchestrationError(
                    "orchestration",
                    "analysis_worker_termination_failed",
                    "background analysis process group could not be stopped safely",
                )

        state_store = AnalysisStateStore(paths.database)
        real_complete_failure = state_store.complete_failure
        failure_committed = threading.Event()

        def commit_then_raise(*arguments):
            real_complete_failure(*arguments)
            failure_committed.set()
            raise RuntimeError("injected post-commit state read failure")

        state_store.complete_failure = commit_then_raise
        scheduler = AnalysisScheduler(
            state_store,
            TerminationFailureOrchestrator,
            active_analysis_check=unresolved_group.is_set,
        )
        barrier_passed = threading.Event()
        barrier_errors = []
        worker_errors = []
        previous_excepthook = threading.excepthook
        threading.excepthook = lambda arguments: worker_errors.append(
            arguments.exc_value
        )
        barrier_thread = None
        scheduler.start()
        try:
            scheduler.request_analysis(cycle_id, "manual_retry")
            self.assertTrue(analysis_started.wait(1))

            def wait_for_idle():
                try:
                    scheduler.pause_and_wait_idle()
                    barrier_passed.set()
                except Exception as error:
                    barrier_errors.append(error)

            barrier_thread = threading.Thread(target=wait_for_idle)
            barrier_thread.start()
            self.assertFalse(barrier_passed.wait(0.1))
            self.assertFalse(failure_committed.is_set())
            self.assertEqual(state_store.get_state(cycle_id)["status"], "running")
            self.assertEqual(scheduler.running_cycle, cycle_id)

            unresolved_group.clear()
            self.assertTrue(failure_committed.wait(1))
            self.assertTrue(barrier_passed.wait(1))
            barrier_thread.join(timeout=1)
            self.assertEqual(barrier_errors, [])
            self.assertEqual(state_store.get_state(cycle_id)["status"], "failed")
            self.assertIsNone(scheduler.running_cycle)
            self.assertEqual(len(worker_errors), 1)
            self.assertIsInstance(worker_errors[0], RuntimeError)
            scheduler.resume()
        finally:
            unresolved_group.clear()
            if barrier_thread is not None and barrier_thread.is_alive():
                scheduler.resume()
                barrier_thread.join(timeout=1)
            scheduler.shutdown(timeout=1)
            threading.excepthook = previous_excepthook

    def test_real_worker_imports_snapshot_and_returns_domain_failure(self):
        """Exercises the production module boundary without requiring LLVM or a Build."""

        paths, registry, cycle_id = self._runtime_cycle()
        orchestrator = ProcessCycleAnalysisOrchestrator(
            paths.root,
            registry.to_document(),
            self.root,
            terminate_grace_seconds=0.05,
            kill_grace_seconds=0.2,
        )
        with self.assertRaises(CycleAnalysisOrchestrationError) as context:
            orchestrator.analyze(cycle_id)
        self.assertEqual(context.exception.code, "current_build_not_found")

    def _runtime_cycle(self):
        """Creates a registered project and one Cycle without Build inputs."""

        repository = self.root / ("ExampleApp-" + str(time.monotonic_ns()))
        subprocess.run(
            ("git", "init", str(repository)),
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        registry = ProjectRegistry.from_document(
            {
                "schema_version": 1,
                "projects": [
                    {
                        "project": "ExampleApp",
                        "display_name": "Example App",
                        "repository_path": str(repository),
                        "gitlab": {
                            "host": "git.example.com",
                            "project_path": "mobile/example-app",
                        },
                        "allowed_bundle_ids": ["com.example.coverage"],
                    }
                ],
            }
        )
        paths = RuntimePaths.from_root(self.root / ("runtime-" + str(time.monotonic_ns())))
        paths.prepare_directories()
        store = CoverageStore(paths.database, paths.data_dir, registry, object())
        cycle_id = store.create_cycle(
            {
                "schema_version": 1,
                "project": "ExampleApp",
                "target_branch": "main",
                "base_sha": "1" * 40,
                "head_sha": "2" * 40,
            }
        )["id"]
        return paths, registry, cycle_id

    def _orchestrator(self, command_factory):
        """Builds a fast-terminating process adapter for one test."""

        return ProcessCycleAnalysisOrchestrator(
            self.root,
            {"schema_version": 1, "projects": []},
            self.root,
            command_factory=command_factory,
            terminate_grace_seconds=0.05,
            kill_grace_seconds=0.2,
        )


if __name__ == "__main__":
    unittest.main()
