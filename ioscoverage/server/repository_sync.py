"""Server-owned periodic and on-demand synchronization of registered MR repositories."""

import logging
import threading
import time
from collections import OrderedDict
from concurrent.futures import Future, TimeoutError

from ioscoverage.integrations.git_sync import GitRepositorySynchronizer, RepositorySyncError
from ioscoverage.server.logging import write_log


DEFAULT_REPOSITORY_SYNC_INTERVAL_SECONDS = 60.0


class RepositorySyncService:
    """Serializes all Git mutations on one worker; the condition protects its queue."""

    def __init__(self, coverage_store, gitlab_client, logger,
                 interval_seconds=DEFAULT_REPOSITORY_SYNC_INTERVAL_SECONDS,
                 runtime_lock_descriptor=None):
        """Captures trusted collaborators without modifying Cycle or analysis state."""

        if interval_seconds <= 0:
            raise ValueError("repository synchronization interval must be positive")
        self.coverage_store = coverage_store
        self.gitlab_client = gitlab_client
        self.logger = logger
        self.interval_seconds = interval_seconds
        self._condition = threading.Condition()
        self._cancellation = threading.Event()
        self._requests = {}
        self._queue = OrderedDict()
        self._worker = None
        self._synchronizer = GitRepositorySynchronizer(
            coverage_store.project_registry, self._cancellation, runtime_lock_descriptor)

    def start(self):
        """Starts an immediate sweep and a periodic timer without blocking HTTP startup."""

        with self._condition:
            if self._worker is not None:
                return
            self._worker = threading.Thread(target=self._run, name="ios-coverage-repository-sync", daemon=False)
            self._worker.start()

    def request_cycle(self, cycle_id, required_shas=()):
        """Coalesces identical queued/running requests; Build repair receives queue priority."""

        key = (cycle_id, tuple(sorted(set(required_shas))))
        with self._condition:
            if self._cancellation.is_set():
                raise RepositorySyncError("repository_sync_stopping", "repository synchronization is stopping")
            future = self._requests.get(key)
            if future is None:
                future = Future()
                self._requests[key] = future
                self._queue[key] = future
            if required_shas and key in self._queue:
                self._queue.move_to_end(key, last=False)
            self._condition.notify_all()
            return future

    def ensure_cycle(self, cycle_id, required_shas):
        """Waits a bounded time for one missing-object repair outside storage locks."""

        future = self.request_cycle(cycle_id, required_shas)
        try:
            future.result(timeout=120.0)
        except TimeoutError:
            raise RepositorySyncError("repository_sync_timeout", "repository synchronization is still pending; retry the request") from None

    def shutdown(self):
        """Cancels Git commands, joins the owner, and refuses to release a live group."""

        with self._condition:
            self._cancellation.set()
            self._condition.notify_all()
            worker = self._worker
        if worker is not None:
            worker.join(timeout=20.0)
        if ((worker is not None and worker.is_alive())
                or self._synchronizer.has_active_process_group()):
            raise RepositorySyncError("repository_sync_termination_failed", "repository synchronization could not be stopped safely")

    def _run(self):
        """Processes requests serially and refreshes both branch tips on every sweep."""

        deadline = 0.0
        try:
            while not self._cancellation.is_set():
                if time.monotonic() >= deadline:
                    self._enqueue_registered_cycles()
                    deadline = time.monotonic() + self.interval_seconds
                with self._condition:
                    if self._cancellation.is_set():
                        break
                    if not self._queue:
                        self._condition.wait(max(0.0, deadline - time.monotonic()))
                        continue
                    key, future = self._queue.popitem(last=False)
                    if not future.set_running_or_notify_cancel():
                        self._requests.pop(key, None)
                        continue
                error = None
                try:
                    self._synchronize_cycle(*key)
                except Exception as caught:
                    code = getattr(caught, "code", "repository_sync_failed")
                    error = caught if isinstance(caught, RepositorySyncError) else RepositorySyncError(
                        code, "registered MR repository could not be synchronized")
                    if code not in ("mr_not_open", "repository_sync_stopping"):
                        write_log(self.logger, "repository_sync_failed", level=logging.WARNING,
                                  cycle_id=key[0], reason=code)
                with self._condition:
                    self._requests.pop(key, None)
                if error is None:
                    future.set_result(None)
                else:
                    future.set_exception(error)
        finally:
            with self._condition:
                pending = list(self._requests.values())
                self._requests.clear()
                self._queue.clear()
            for future in pending:
                if not future.done():
                    future.set_exception(RepositorySyncError(
                        "repository_sync_stopping", "repository synchronization is stopping"))

    def _enqueue_registered_cycles(self):
        """Queues persisted MR identities; the worker rejects unregistered projects individually."""

        try:
            cycles = self.coverage_store.list_repository_sync_cycles()
            for cycle in cycles:
                if self._cancellation.is_set():
                    return
                if cycle.get("mr_iid") and cycle.get("mr_url"):
                    self.request_cycle(str(cycle["id"]))
        except Exception as error:
            write_log(self.logger, "repository_sync_scan_failed", level=logging.WARNING,
                      reason=getattr(error, "code", "repository_sync_failed"))

    def _synchronize_cycle(self, cycle_id, required_shas):
        """Re-queries registry-bounded MR identity and synchronizes without advancing head."""

        cycle = self.coverage_store.get_cycle(cycle_id)
        merge_request = self.gitlab_client.fetch(cycle.get("mr_url"))
        if (merge_request.project != cycle["project"]
                or str(merge_request.iid) != str(cycle.get("mr_iid"))):
            raise RepositorySyncError("repository_sync_mr_mismatch", "MR does not match the registered Cycle")
        self._synchronizer.synchronize(merge_request, required_shas)
        write_log(self.logger, "repository_synchronized", cycle_id=cycle_id,
                  project=merge_request.project)
