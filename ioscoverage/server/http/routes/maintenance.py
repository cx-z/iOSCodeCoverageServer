"""Operator-only runtime maintenance routes."""

from http import HTTPStatus

from ioscoverage.lifecycle.manager import RuntimeCleanupManagerError


class MaintenanceRoutesMixin:
    """Exposes bounded cleanup status and asynchronous manual triggering."""

    def _handle_cleanup_get(self) -> None:
        """Returns in-memory cleanup state without paths or credentials."""

        manager = self.config.runtime_cleanup_manager
        if manager is None:
            self._send_json(
                HTTPStatus.SERVICE_UNAVAILABLE,
                {"error": "runtime_cleanup_unavailable"},
            )
            return
        self._send_json(HTTPStatus.OK, manager.get_state())

    def _handle_cleanup_post(self) -> None:
        """Queues a manual cleanup without waiting for deletion to finish."""

        if not self._has_valid_empty_body_framing():
            self._send_json(
                HTTPStatus.BAD_REQUEST,
                {
                    "error": "request_body_not_allowed",
                    "message": "cleanup accepts no request body",
                },
            )
            return
        manager = self.config.runtime_cleanup_manager
        if manager is None:
            self._send_json(
                HTTPStatus.SERVICE_UNAVAILABLE,
                {"error": "runtime_cleanup_unavailable"},
            )
            return
        try:
            state = manager.request_cleanup()
        except RuntimeCleanupManagerError as error:
            self._send_json(
                HTTPStatus.SERVICE_UNAVAILABLE,
                {"error": error.code, "message": error.message},
            )
            return
        self._log_event("runtime_cleanup_requested", cleanup_status=state["status"])
        self._send_json(HTTPStatus.ACCEPTED, state)
