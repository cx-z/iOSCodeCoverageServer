"""Public health route."""

from datetime import datetime, timezone
from http import HTTPStatus

from ioscoverage.server.config import SERVER_VERSION


class HealthRoutesMixin:
    """Provides the probe-safe unauthenticated liveness response."""

    def _handle_health(self) -> None:
        """Returns only stable public health fields."""

        self._send_json(
            HTTPStatus.OK,
            {
                "status": "ok",
                "server_version": SERVER_VERSION,
                "server_time": datetime.now(timezone.utc).isoformat(),
                "instance_id": self.config.instance_id,
            },
        )
