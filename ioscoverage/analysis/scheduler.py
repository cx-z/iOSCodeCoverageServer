#!/usr/bin/env python3
"""Single-worker background coordinator for Cycle orchestration."""

import threading
from collections import deque
from typing import Callable, Deque, Dict, Optional, Set

from ioscoverage.storage.analysis_state import AnalysisStateStore
from ioscoverage.analysis.orchestrator import CycleAnalysisOrchestrationError


class AnalysisSchedulerError(Exception):
    """Carries one stable scheduler lifecycle or request error."""

    def __init__(self, code: str, message: str):
        """Creates one safe scheduler error."""

        super().__init__(message)
        self.code = code
        self.message = message


class AnalysisScheduler:
    """Owns one process-global queue, worker thread, and stop lifecycle."""

    def __init__(
        self,
        state_store: AnalysisStateStore,
        orchestrator_factory: Callable[[], object],
        thread_name: str = "ios-coverage-analysis-worker",
    ):
        """Creates synchronized in-memory notification state around persisted generations."""

        self.state_store = state_store
        self.orchestrator_factory = orchestrator_factory
        self.thread_name = thread_name
        self._condition = threading.Condition()
        self._queue: Deque[str] = deque()
        self._queued: Set[str] = set()
        self._stopping = False
        self._started = False
        self._running_cycle: Optional[str] = None
        self._worker: Optional[threading.Thread] = None

    def start(self) -> None:
        """Recovers persisted work and starts the only analysis worker."""

        with self._condition:
            if self._started:
                return
            self._started = True
        try:
            self.state_store.recover_interrupted()
            queued = self.state_store.queued_cycles()
        except Exception:
            with self._condition:
                self._started = False
            raise
        with self._condition:
            for cycle_id in queued:
                self._enqueue_locked(cycle_id)
            self._worker = threading.Thread(
                target=self._run, name=self.thread_name, daemon=False
            )
            self._worker.start()

    def request_analysis(
        self, cycle_id: str, trigger: str, latest_run_id: Optional[str] = None
    ) -> Dict[str, object]:
        """Persists a new generation, then sends a narrow notification to the worker."""

        with self._condition:
            if self._stopping:
                raise AnalysisSchedulerError(
                    "analysis_scheduler_stopping", "analysis scheduler is stopping"
                )
        state = self.state_store.request_analysis(cycle_id, trigger, latest_run_id)
        with self._condition:
            if self._stopping:
                return state
            self._enqueue_locked(cycle_id)
            self._condition.notify()
        return state

    def get_state(self, cycle_id: str) -> Dict[str, object]:
        """Returns persisted state without exposing mutable queue containers."""

        return self.state_store.get_state(cycle_id)

    def shutdown(self, timeout: float = 30.0) -> bool:
        """Stops accepting work and waits bounded time for the current immutable publish."""

        with self._condition:
            if not self._started:
                self._stopping = True
                return True
            self._stopping = True
            self._condition.notify_all()
            worker = self._worker
        if worker is not None and worker is not threading.current_thread():
            worker.join(timeout=max(0.0, timeout))
            return not worker.is_alive()
        return True

    @property
    def running_cycle(self) -> Optional[str]:
        """Returns a lock-protected diagnostic snapshot of the worker owner."""

        with self._condition:
            return self._running_cycle

    def _run(self) -> None:
        """Claims persisted generations serially and never holds the condition during work."""

        while True:
            with self._condition:
                while not self._stopping and not self._queue:
                    self._condition.wait()
                if self._stopping:
                    return
                cycle_id = self._queue.popleft()
                self._queued.discard(cycle_id)
                self._running_cycle = cycle_id
            try:
                generation = self.state_store.claim(cycle_id)
                if generation is None:
                    continue
                try:
                    orchestrator = self.orchestrator_factory()
                    result = orchestrator.analyze(cycle_id)
                    state = self.state_store.complete_success(
                        cycle_id, generation, self._result_document(result)
                    )
                except CycleAnalysisOrchestrationError as error:
                    state = self.state_store.complete_failure(
                        cycle_id,
                        generation,
                        error.stage,
                        error.code,
                        error.message,
                    )
                except Exception:
                    state = self.state_store.complete_failure(
                        cycle_id,
                        generation,
                        "orchestration",
                        "analysis_internal_error",
                        "background Cycle analysis failed unexpectedly",
                    )
                if state["status"] == "queued":
                    with self._condition:
                        self._enqueue_locked(cycle_id)
                        self._condition.notify()
            finally:
                with self._condition:
                    if self._running_cycle == cycle_id:
                        self._running_cycle = None

    def _enqueue_locked(self, cycle_id: str) -> None:
        """Coalesces repeated notifications while the condition lock is held."""

        if cycle_id not in self._queued:
            self._queue.append(cycle_id)
            self._queued.add(cycle_id)

    def _result_document(self, result: object) -> Dict[str, object]:
        """Projects orchestration output into small persistent references and report counters."""

        summary = result.summary
        cumulative = summary["cumulative_summary"]
        lines = cumulative["lines"]
        projection = {
            "changed_new_side": lines["changed_new_side"],
            "measurable": lines["measurable"],
            "covered": lines["covered"],
            "uncovered": lines["uncovered"],
            "not_measurable": lines["not_measurable"],
            "unknown": lines["unknown"],
            "deleted_excluded": lines["deleted_excluded"],
            "covered_with_inherited_evidence": lines["covered_with_inherited_evidence"],
            "inherited": lines["covered_with_inherited_evidence"],
            "changed_line_coverage_percent": cumulative["changed_line_coverage_percent"],
        }
        return {
            "orchestration_input_sha256": result.orchestration_input_sha256,
            "cycle_input_set_sha256": summary["cycle_input_set_sha256"],
            "current_build_id": summary["current_build_id"],
            "output_directory": str(result.output_directory),
            "summary": projection,
        }
