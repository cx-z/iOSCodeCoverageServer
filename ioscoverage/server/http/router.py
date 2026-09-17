"""Route matching for the standard-library request handler."""

import re
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler
from urllib.parse import urlsplit

from ioscoverage.server.config import ProbeConfig, SERVER_VERSION
from ioscoverage.server.http.auth import AuthenticationMixin
from ioscoverage.server.http.errors import ErrorResponseMixin
from ioscoverage.server.http.multipart import MultipartMixin
from ioscoverage.server.http.request import RequestMixin
from ioscoverage.server.http.request_logging import RequestLoggingMixin
from ioscoverage.server.http.response import ResponseMixin
from ioscoverage.server.http.routes.analysis import AnalysisRoutesMixin
from ioscoverage.server.http.routes.builds import BuildRoutesMixin
from ioscoverage.server.http.routes.cycles import CycleRoutesMixin
from ioscoverage.server.http.routes.health import HealthRoutesMixin
from ioscoverage.server.http.routes.integrations import IntegrationRoutesMixin
from ioscoverage.server.http.routes.maintenance import MaintenanceRoutesMixin
from ioscoverage.server.http.routes.pages import PageRoutesMixin
from ioscoverage.server.http.routes.probes import ProbeRoutesMixin
from ioscoverage.server.http.routes.reports import ReportRoutesMixin
from ioscoverage.server.http.routes.runs import RunRoutesMixin
from ioscoverage.web.pages.cycles import render_cycle_list_page


class ProbeRequestHandler(
    AuthenticationMixin,
    RequestMixin,
    MultipartMixin,
    ResponseMixin,
    ErrorResponseMixin,
    RequestLoggingMixin,
    HealthRoutesMixin,
    CycleRoutesMixin,
    BuildRoutesMixin,
    RunRoutesMixin,
    AnalysisRoutesMixin,
    ReportRoutesMixin,
    IntegrationRoutesMixin,
    MaintenanceRoutesMixin,
    ProbeRoutesMixin,
    PageRoutesMixin,
    BaseHTTPRequestHandler,
):
    """Matches stable paths before delegating to resource route mixins."""
    @property
    def config(self) -> ProbeConfig:
        """Returns the immutable configuration owned by the HTTP server."""

        return self.server.config  # type: ignore[attr-defined]
    def do_GET(self) -> None:
        """Routes public reads and authenticated resource queries."""

        self._begin_request()
        path = urlsplit(self.path).path
        if path == "/health":
            self._handle_health()
            return
        if path == "/":
            self._send_html(HTTPStatus.OK, self._render_upload_page())
            return
        if path == "/cycles":
            self._send_redirect("/cycles/")
            return
        if path == "/cycles/":
            self._send_html(HTTPStatus.OK, render_cycle_list_page())
            return
        report_page_match = re.fullmatch(r"/cycles/([^/]+)", path)
        if report_page_match:
            self._handle_report_page(report_page_match.group(1))
            return
        if path == "/api/v1/cycles":
            if not self._require_token():
                return
            self._handle_cycle_list()
            return
        if path == "/api/v1/maintenance/cleanup":
            if not self._require_token():
                return
            self._handle_cleanup_get()
            return
        analysis_match = re.fullmatch(r"/api/v1/cycles/([^/]+)/analysis", path)
        if analysis_match:
            if not self._require_token():
                return
            self._handle_analysis_get(analysis_match.group(1))
            return
        report_file_match = re.fullmatch(
            r"/api/v1/cycles/([^/]+)/report/files/([0-9a-f]{64})", path
        )
        if report_file_match:
            if not self._authorize_read():
                return
            self._handle_report_file_get(
                report_file_match.group(1), report_file_match.group(2)
            )
            return
        report_match = re.fullmatch(r"/api/v1/cycles/([^/]+)/report", path)
        if report_match:
            if not self._authorize_read():
                return
            self._handle_report_get(report_match.group(1))
            return
        cycle_match = re.fullmatch(r"/api/v1/cycles/([^/]+)", path)
        if cycle_match:
            if not self._require_token():
                return
            self._handle_cycle_get(cycle_match.group(1))
            return
        build_match = re.fullmatch(r"/api/v1/builds/([^/]+)", path)
        if build_match:
            if not self._require_token():
                return
            self._handle_build_get(build_match.group(1))
            return
        run_match = re.fullmatch(r"/api/v1/runs/([^/]+)", path)
        if run_match:
            if not self._require_token():
                return
            self._handle_run_get(run_match.group(1))
            return
        self._send_json(HTTPStatus.NOT_FOUND, {"error": "not_found"})
    def do_POST(self) -> None:
        """Routes authenticated probe, resource creation, and upload operations."""

        self._begin_request()
        path = urlsplit(self.path).path
        if path == "/api/v1/integrations/gitlab/merge-request-coverage/resolve":
            if not self._authorize_read():
                return
            self._handle_gitlab_merge_request_coverage_resolve()
            return
        retry_match = re.fullmatch(r"/api/v1/cycles/([^/]+)/analysis/retry", path)
        if retry_match:
            if not self._require_token():
                return
            self._handle_analysis_retry(retry_match.group(1))
            return
        if path == "/api/v1/maintenance/cleanup":
            if not self._require_token():
                return
            self._handle_cleanup_post()
            return
        if path not in (
            "/api/v1/probes",
            "/api/v1/cycles",
            "/api/v1/builds",
            "/api/v1/runs",
        ):
            self._send_json(HTTPStatus.NOT_FOUND, {"error": "not_found"})
            return
        allow_run_upload = path in ("/api/v1/probes", "/api/v1/runs")
        if not self._require_token(allow_run_upload=allow_run_upload):
            return
        if path == "/api/v1/probes":
            self._handle_probe_upload()
        elif path == "/api/v1/cycles":
            self._handle_cycle_create()
        elif path == "/api/v1/builds":
            self._handle_build_register()
        else:
            self._handle_run_upload()
    def do_PATCH(self) -> None:
        """Allows only an authenticated Cycle head_sha update."""

        self._begin_request()
        match = re.fullmatch(r"/api/v1/cycles/([^/]+)", urlsplit(self.path).path)
        if match is None:
            self._send_json(HTTPStatus.NOT_FOUND, {"error": "not_found"})
            return
        if not self._require_token():
            return
        self._handle_cycle_update(match.group(1))
