"""Startup, periodic, and manual scheduling for coordinated runtime cleanup."""

import copy
import logging
import threading
import time
from datetime import datetime, timezone
from typing import Dict, Optional

from ioscoverage.analysis.scheduler import AnalysisScheduler
from ioscoverage.lifecycle.cleanup import RuntimeCleanupError, RuntimeDataCleaner
from ioscoverage.lifecycle.runtime_lock import RuntimeProcessLock
from ioscoverage.server.logging import write_log
from ioscoverage.storage.coverage_store import CoverageStore


DEFAULT_CLEANUP_INTERVAL_SECONDS = 24 * 60 * 60
MAX_SKIPPED_STATUS_ITEMS = 20


class RuntimeCleanupManagerError(RuntimeError):
    """Carries one stable cleanup-manager request error."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


class RuntimeCleanupManager:
    """Owns one cleanup worker and a timer reset by each service process start."""

    def __init__(
        self,
        cleaner: RuntimeDataCleaner,
        process_lock: RuntimeProcessLock,
        analysis_scheduler: AnalysisScheduler,
        coverage_store: CoverageStore,
        logger: logging.Logger,
        interval_seconds: float = DEFAULT_CLEANUP_INTERVAL_SECONDS,
        thread_name: str = "ios-coverage-cleanup-worker",
    ):
        if interval_seconds <= 0:
            raise ValueError("cleanup interval must be positive")
        self.cleaner = cleaner
        self.process_lock = process_lock
        self.analysis_scheduler = analysis_scheduler
        self.coverage_store = coverage_store
        self.logger = logger
        self.interval_seconds = float(interval_seconds)
        self.thread_name = thread_name
        self._condition = threading.Condition()
        self._cancellation_event = threading.Event()
        self._started = False
        self._stopping = False
        self._requested = False
        self._running = False
        self._requested_count = 0
        self._completed_count = 0
        self._last_trigger: Optional[str] = None
        self._last_started_at: Optional[str] = None
        self._last_completed_at: Optional[str] = None
        self._last_result: Optional[Dict[str, object]] = None
        self._last_error: Optional[Dict[str, str]] = None
        self._worker: Optional[threading.Thread] = None

    def run_startup_cleanup(self) -> Dict[str, object]:
        """Synchronously attempts cleanup before analysis and HTTP serving begin."""

        with self._condition:
            if self._started:
                raise RuntimeCleanupManagerError(
                    "cleanup_already_started", "cleanup manager has already started"
                )
            self._requested_count += 1
            self._running = True
            self._last_trigger = "startup"
            self._last_started_at = self._utc_now()
        return self._execute("startup")

    def start(self) -> None:
        """Starts the in-memory 24-hour timer after the startup attempt."""

        with self._condition:
            if self._started:
                return
            self._started = True
            self._worker = threading.Thread(
                target=self._run, name=self.thread_name, daemon=False
            )
            self._worker.start()

    def request_cleanup(self) -> Dict[str, object]:
        """Coalesces one manual asynchronous request without blocking its caller."""

        with self._condition:
            if self._stopping:
                raise RuntimeCleanupManagerError(
                    "cleanup_manager_stopping", "cleanup manager is stopping"
                )
            self._requested_count += 1
            self._requested = True
            self._condition.notify_all()
            return self._state_locked()

    def get_state(self) -> Dict[str, object]:
        """Returns bounded operational state without runtime paths or credentials."""

        with self._condition:
            return self._state_locked()

    def shutdown(self) -> None:
        """Stops future work and lets any quarantined deletion finish safely."""

        with self._condition:
            self._stopping = True
            self._requested = False
            self._cancellation_event.set()
            self._condition.notify_all()
            worker = self._worker
        if worker is not None and worker is not threading.current_thread():
            worker.join()

    def _run(self) -> None:
        """Runs on manual notification or after each full periodic interval."""

        deadline = time.monotonic() + self.interval_seconds
        while True:
            with self._condition:
                while not self._stopping and not self._requested:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        break
                    self._condition.wait(remaining)
                if self._stopping:
                    return
                if self._requested:
                    trigger = "manual"
                    self._requested = False
                else:
                    trigger = "periodic"
                    self._requested_count += 1
                self._running = True
                self._last_trigger = trigger
                self._last_started_at = self._utc_now()
            self._execute(trigger)
            if trigger == "periodic":
                deadline = time.monotonic() + self.interval_seconds

    def _execute(self, trigger: str) -> Dict[str, object]:
        """Delegates the complete safety barrier and deletion to the cleaner."""

        try:
            result = self.cleaner.run_online(
                self.process_lock,
                self.analysis_scheduler,
                self.coverage_store,
                self._cancellation_event,
            )
            projection = self._result_projection(result)
            deleted_items = sum(
                int(value["items"])
                for value in result["actual_deletion"].values()
            )
        except RuntimeCleanupError as error:
            return self._record_failure(trigger, error.code, error.message)
        except Exception:
            return self._record_failure(
                trigger, "cleanup_internal_error", "runtime cleanup failed unexpectedly"
            )
        with self._condition:
            self._running = False
            self._completed_count += 1
            self._last_completed_at = self._utc_now()
            self._last_result = projection
            self._last_error = None
            state = self._state_locked()
            self._condition.notify_all()
        write_log(
            self.logger,
            "runtime_cleanup_completed",
            trigger=trigger,
            deleted_items=deleted_items,
            released_bytes=result["released_bytes"],
        )
        return state

    def _record_failure(self, trigger: str, code: str, message: str) -> Dict[str, object]:
        """Records a bounded failure without stopping HTTP or losing future timer work."""

        with self._condition:
            self._running = False
            self._completed_count += 1
            self._last_completed_at = self._utc_now()
            self._last_result = None
            self._last_error = {"code": code, "message": message}
            state = self._state_locked()
            self._condition.notify_all()
        write_log(
            self.logger,
            "runtime_cleanup_failed",
            level=logging.ERROR,
            trigger=trigger,
            reason=code,
        )
        return state

    def _state_locked(self) -> Dict[str, object]:
        """Builds one immutable-by-copy state document while holding the condition."""

        if self._running:
            status = "running"
        elif self._requested:
            status = "queued"
        elif self._last_error is not None:
            status = "failed"
        elif self._last_completed_at is not None:
            status = "succeeded"
        else:
            status = "idle"
        return {
            "status": status,
            "requested_count": self._requested_count,
            "completed_count": self._completed_count,
            "last_trigger": self._last_trigger,
            "pending_trigger": "manual" if self._requested else None,
            "last_started_at": self._last_started_at,
            "last_completed_at": self._last_completed_at,
            "interval_seconds": int(self.interval_seconds),
            "last_result": copy.deepcopy(self._last_result),
            "error": copy.deepcopy(self._last_error),
        }

    @staticmethod
    def _result_projection(result: Dict[str, object]) -> Dict[str, object]:
        """Keeps only aggregate cleanup counters suitable for HTTP and logs."""

        return {
            "dry_run": result["dry_run"],
            "planned_deletion": copy.deepcopy(result["planned_deletion"]),
            "actual_deletion": copy.deepcopy(result["actual_deletion"]),
            "planned_release_bytes": result["planned_release_bytes"],
            "released_bytes": result["released_bytes"],
            "skipped_count": result["skipped_count"],
            "skipped": copy.deepcopy(result["skipped"][:MAX_SKIPPED_STATUS_ITEMS]),
        }

    @staticmethod
    def _utc_now() -> str:
        return datetime.now(timezone.utc).isoformat()
