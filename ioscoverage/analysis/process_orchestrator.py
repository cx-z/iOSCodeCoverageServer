#!/usr/bin/env python3
"""Process-isolated production boundary for one Cycle orchestration attempt."""

import json
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Callable, Mapping, Optional, Sequence

from ioscoverage.analysis.orchestrator import (
    CycleAnalysisOrchestrationError,
    CycleAnalysisOrchestrationResult,
)
from ioscoverage.lifecycle.runtime_lock import RUNTIME_LOCK_LEASE_FD_ENV


MAX_WORKER_RESPONSE_BYTES = 4 * 1024 * 1024
MAX_REGISTRY_SNAPSHOT_BYTES = 1024 * 1024
TERMINATE_GRACE_SECONDS = 2.0
KILL_GRACE_SECONDS = 2.0


class ProcessCycleAnalysisOrchestrator:
    """Runs the existing orchestrator in one terminable child process group."""

    def __init__(
        self,
        runtime_root: Path,
        project_registry_document: Mapping[str, object],
        developer_dir: Path,
        command_factory: Optional[Callable[[str], Sequence[str]]] = None,
        terminate_grace_seconds: float = TERMINATE_GRACE_SECONDS,
        kill_grace_seconds: float = KILL_GRACE_SECONDS,
        runtime_lock_descriptor: Optional[int] = None,
    ):
        """Captures trusted startup configuration and an optional test command boundary."""

        self.runtime_root = runtime_root.resolve()
        self.project_registry_json = json.dumps(
            project_registry_document, sort_keys=True, separators=(",", ":")
        )
        if len(self.project_registry_json.encode("utf-8")) > MAX_REGISTRY_SNAPSHOT_BYTES:
            raise ValueError("project registry snapshot is too large")
        self.developer_dir = developer_dir.resolve()
        self.command_factory = command_factory or self._worker_command
        self.terminate_grace_seconds = max(0.0, terminate_grace_seconds)
        self.kill_grace_seconds = max(0.0, kill_grace_seconds)
        if runtime_lock_descriptor is not None:
            try:
                os.fstat(runtime_lock_descriptor)
            except OSError as error:
                raise ValueError("runtime lock descriptor is invalid") from error
        self.runtime_lock_descriptor = runtime_lock_descriptor
        self._lock = threading.Lock()
        self._active: Optional[subprocess.Popen] = None
        self._stopping = False

    def analyze(self, cycle_id: str) -> CycleAnalysisOrchestrationResult:
        """Executes one attempt without allowing shutdown to leave child commands alive."""

        with self._lock:
            if self._stopping:
                raise self._cancelled_error()
            if self._active is not None:
                if self._group_exists(self._active.pid):
                    raise self._termination_failed_error()
                self._active = None
            command = list(self.command_factory(cycle_id))
            environment = dict(os.environ)
            environment.pop(RUNTIME_LOCK_LEASE_FD_ENV, None)
            inherited_descriptors = ()
            if self.runtime_lock_descriptor is not None:
                environment[RUNTIME_LOCK_LEASE_FD_ENV] = str(
                    self.runtime_lock_descriptor
                )
                inherited_descriptors = (self.runtime_lock_descriptor,)
            try:
                process = subprocess.Popen(
                    command,
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    start_new_session=True,
                    close_fds=True,
                    pass_fds=inherited_descriptors,
                    env=environment,
                )
            except OSError as error:
                raise CycleAnalysisOrchestrationError(
                    "orchestration",
                    "analysis_worker_start_failed",
                    "background analysis process could not be started",
                ) from error
            self._active = process
        transport_error: Optional[Exception] = None
        try:
            stdout, _stderr = process.communicate(input=self.project_registry_json)
        except Exception as error:
            stdout = ""
            transport_error = error

        descendants_lingered = self._group_exists(process.pid)
        termination_error: Optional[Exception] = None
        if descendants_lingered:
            try:
                self._stop_process_group(process)
            except Exception as error:
                termination_error = error
        group_stopped = not self._group_exists(process.pid)
        if group_stopped:
            with self._lock:
                if self._active is process:
                    self._active = None
        if termination_error is not None or not group_stopped:
            raise self._termination_failed_error() from termination_error

        with self._lock:
            stopping = self._stopping
        if stopping and (process.returncode != 0 or descendants_lingered):
            raise self._cancelled_error()
        if descendants_lingered:
            raise CycleAnalysisOrchestrationError(
                "orchestration",
                "analysis_worker_descendants_leaked",
                "background analysis worker exited before its child commands",
            )
        if transport_error is not None:
            raise self._invalid_response_error() from transport_error
        payload = self._parse_response(stdout)
        if process.returncode != 0 or payload.get("status") != "complete":
            if payload.get("status") == "failed":
                raise CycleAnalysisOrchestrationError(
                    str(payload.get("stage") or "orchestration"),
                    str(payload.get("code") or "analysis_worker_failed"),
                    str(payload.get("message") or "background Cycle analysis failed"),
                )
            raise CycleAnalysisOrchestrationError(
                "orchestration",
                "analysis_worker_failed",
                "background analysis process exited without a valid result",
            )
        result = payload.get("result")
        if not isinstance(result, dict):
            raise self._invalid_response_error()
        identity = result.get("orchestration_input_sha256")
        output_directory = result.get("output_directory")
        summary = result.get("summary")
        replay = result.get("idempotent_replay")
        if (
            not isinstance(identity, str)
            or len(identity) != 64
            or not isinstance(output_directory, str)
            or not output_directory
            or not isinstance(summary, dict)
            or not isinstance(replay, bool)
        ):
            raise self._invalid_response_error()
        return CycleAnalysisOrchestrationResult(
            identity,
            Path(output_directory),
            summary,
            replay,
        )

    def cancel_active(self) -> None:
        """Terminates the active child process group within a fixed grace period."""

        with self._lock:
            self._stopping = True
            process = self._active
        if process is None:
            return
        if not self._group_exists(process.pid):
            with self._lock:
                if self._active is process:
                    self._active = None
            return
        self._stop_process_group(process)

    def has_active_process_group(self) -> bool:
        """Reports and clears only a process group proven to contain no members."""

        with self._lock:
            process = self._active
        if process is None:
            return False
        if self._group_exists(process.pid):
            return True
        with self._lock:
            if self._active is process:
                self._active = None
        return False

    def _stop_process_group(self, process: subprocess.Popen) -> None:
        """Terminates and confirms every process in one owned execution group."""

        self._signal_group(process, signal.SIGTERM)
        if self._wait_for_group_exit(process.pid, self.terminate_grace_seconds):
            return
        self._signal_group(process, signal.SIGKILL)
        if not self._wait_for_group_exit(process.pid, self.kill_grace_seconds):
            raise RuntimeError("background analysis process could not be terminated")

    def _worker_command(self, cycle_id: str) -> Sequence[str]:
        """Builds the fixed internal worker argv without a shell or caller-controlled path."""

        return (
            sys.executable,
            "-m",
            "ioscoverage.analysis.process_worker",
            "--runtime-root",
            str(self.runtime_root),
            "--developer-dir",
            str(self.developer_dir),
            "--cycle-id",
            cycle_id,
        )

    def _parse_response(self, stdout: str) -> Mapping[str, object]:
        """Accepts exactly one bounded JSON document from the private worker."""

        encoded = stdout.encode("utf-8")
        if not encoded or len(encoded) > MAX_WORKER_RESPONSE_BYTES:
            raise self._invalid_response_error()
        try:
            payload = json.loads(stdout)
        except (TypeError, ValueError) as error:
            raise self._invalid_response_error() from error
        if not isinstance(payload, dict):
            raise self._invalid_response_error()
        return payload

    @staticmethod
    def _signal_group(process: subprocess.Popen, signal_number: int) -> None:
        """Signals only the new process group owned by this execution boundary."""

        try:
            os.killpg(process.pid, signal_number)
        except ProcessLookupError:
            return

    @staticmethod
    def _group_exists(process_group_id: int) -> bool:
        """Checks whether any process remains in the owned execution group."""

        try:
            os.killpg(process_group_id, 0)
        except ProcessLookupError:
            return False
        return True

    @classmethod
    def _wait_for_group_exit(cls, process_group_id: int, timeout: float) -> bool:
        """Waits only to a deadline for every child command in the group to exit."""

        deadline = time.monotonic() + timeout
        while cls._group_exists(process_group_id) and time.monotonic() < deadline:
            time.sleep(0.01)
        return not cls._group_exists(process_group_id)

    @staticmethod
    def _cancelled_error() -> CycleAnalysisOrchestrationError:
        """Returns the stable internal cancellation used only during server shutdown."""

        return CycleAnalysisOrchestrationError(
            "orchestration",
            "analysis_cancelled",
            "background Cycle analysis was cancelled during server shutdown",
        )

    @staticmethod
    def _invalid_response_error() -> CycleAnalysisOrchestrationError:
        """Returns a stable error without exposing child stderr or private paths."""

        return CycleAnalysisOrchestrationError(
            "orchestration",
            "analysis_worker_invalid_response",
            "background analysis process returned an invalid result",
        )

    @staticmethod
    def _termination_failed_error() -> CycleAnalysisOrchestrationError:
        """Returns the fail-closed error while process-group ownership is unresolved."""

        return CycleAnalysisOrchestrationError(
            "orchestration",
            "analysis_worker_termination_failed",
            "background analysis process group could not be stopped safely",
        )
