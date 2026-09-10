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

    def __init__(self, server_address: Tuple[str, int], config: ProbeConfig):
        """Binds HTTP before starting the owned background scheduler."""

        self.config = config
        super().__init__(server_address, ProbeRequestHandler)
        try:
            if self.config.runtime_cleanup_manager is not None:
                self.config.analysis_state_store.recover_interrupted()
                self.config.runtime_cleanup_manager.run_startup_cleanup()
            if self.config.analysis_scheduler is not None:
                self.config.analysis_scheduler.start()
            if self.config.runtime_cleanup_manager is not None:
                self.config.runtime_cleanup_manager.start()
        except Exception:
            if self.config.analysis_scheduler is not None:
                completed = self.config.analysis_scheduler.shutdown(timeout=30.0)
            else:
                completed = True
            super().server_close()
            if not completed:
                raise AnalysisSchedulerError(
                    "analysis_shutdown_incomplete",
                    "background analysis could not be stopped safely",
                )
            raise

    def server_close(self) -> None:
        """Closes the listener but retains lifecycle ownership until workers stop."""

        completed = True
        try:
            if self.config.runtime_cleanup_manager is not None:
                self.config.runtime_cleanup_manager.shutdown()
            if self.config.analysis_scheduler is not None:
                completed = self.config.analysis_scheduler.shutdown(timeout=30.0)
                if not completed:
                    write_log(
                        self.config.logger,
                        "analysis_worker_shutdown_incomplete",
                        level=logging.WARNING,
                        running_cycle=self.config.analysis_scheduler.running_cycle,
                    )
        finally:
            super().server_close()
        if not completed:
            raise AnalysisSchedulerError(
                "analysis_shutdown_incomplete",
                "background analysis could not be stopped safely",
            )

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
