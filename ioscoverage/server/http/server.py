"""HTTP server lifecycle and background-worker ownership."""

import logging
import traceback
from http.server import ThreadingHTTPServer
from typing import Tuple

from ioscoverage.analysis.scheduler import AnalysisSchedulerError
from ioscoverage.server.config import ProbeConfig
from ioscoverage.server.http.router import ProbeRequestHandler
from ioscoverage.server.logging import write_log


class ProbeHTTPServer(ThreadingHTTPServer):
    """Adds immutable probe configuration to the standard threaded server."""

    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, server_address: Tuple[str, int], config: ProbeConfig, termination_requested=None):
        """Binds under deferred signals; finishes startup cleanup before honoring stop intent."""

        self.config = config
        self._termination_requested = termination_requested or (lambda: False)
        super().__init__(server_address, ProbeRequestHandler)
        try:
            if not self._termination_requested() and self.config.runtime_cleanup_manager is not None:
                self.config.analysis_state_store.recover_interrupted()
                self.config.runtime_cleanup_manager.run_startup_cleanup()
            for worker in (self.config.analysis_scheduler, self.config.runtime_cleanup_manager,
                           self.config.repository_sync_service):
                if self._termination_requested():
                    break
                if worker is not None:
                    worker.start()
        except BaseException:
            # Also unwind KeyboardInterrupt/SystemExit from collaborators or bind failure.
            self.server_close()
            raise

    def service_actions(self):
        """Honors stop intent only at the serve loop safe-point, never inside deletion."""
        if self._termination_requested():
            raise KeyboardInterrupt
        super().service_actions()

    def server_close(self) -> None:
        """Attempts every owner shutdown, closes the socket and propagates unsafe teardown failures."""

        errors = []
        for owner in (self.config.repository_sync_service, self.config.runtime_cleanup_manager):
            if owner is not None:
                try:
                    owner.shutdown()
                except BaseException as error:
                    errors.append(error)
        try:
            if self.config.analysis_scheduler is not None:
                try:
                    completed = self.config.analysis_scheduler.shutdown(timeout=30.0)
                    if not completed:
                        write_log(self.config.logger, "analysis_worker_shutdown_incomplete",
                                  level=logging.WARNING,
                                  running_cycle=self.config.analysis_scheduler.running_cycle)
                        errors.append(AnalysisSchedulerError(
                            "analysis_shutdown_incomplete", "background analysis could not be stopped safely"))
                except BaseException as error:
                    errors.append(error)
        finally:
            super().server_close()
        if errors:
            raise errors[0]

    def handle_error(self, request: object, client_address: Tuple[str, int]) -> None:
        """Persists uncaught request failures so terminal access is not required."""

        write_log(
            self.config.logger,
            "request_exception",
            level=logging.ERROR,
            client_ip=client_address[0],
            client_port=client_address[1],
            traceback=traceback.format_exc(),
        )
