"""HTTP server lifecycle and background-worker ownership."""

import logging
import traceback
from http.server import ThreadingHTTPServer
from typing import Tuple

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
            if self.config.analysis_scheduler is not None:
                self.config.analysis_scheduler.start()
        except Exception:
            super().server_close()
            raise

    def server_close(self) -> None:
        """Stops scheduling and boundedly waits for an in-flight immutable publication."""

        try:
            if self.config.analysis_scheduler is not None:
                completed = self.config.analysis_scheduler.shutdown(timeout=30.0)
                if not completed:
                    write_log(
                        self.config.logger,
                        "analysis_worker_shutdown_deferred",
                        level=logging.WARNING,
                        running_cycle=self.config.analysis_scheduler.running_cycle,
                    )
        finally:
            super().server_close()

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
