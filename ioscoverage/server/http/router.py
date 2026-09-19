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
from ioscoverage.server.http.routes.accounts import AccountRoutesMixin
from ioscoverage.server.http.routes.builds import BuildRoutesMixin
from ioscoverage.server.http.routes.cycles import CycleRoutesMixin
from ioscoverage.server.http.routes.health import HealthRoutesMixin
from ioscoverage.server.http.routes.integrations import IntegrationRoutesMixin
from ioscoverage.server.http.routes.maintenance import MaintenanceRoutesMixin
from ioscoverage.server.http.routes.pages import PageRoutesMixin
from ioscoverage.server.http.routes.probes import ProbeRoutesMixin
from ioscoverage.server.http.routes.reports import ReportRoutesMixin
from ioscoverage.server.http.routes.runs import RunRoutesMixin


class ProbeRequestHandler(
    AuthenticationMixin,
    RequestMixin,
    MultipartMixin,
    ResponseMixin,
    ErrorResponseMixin,
    RequestLoggingMixin,
    HealthRoutesMixin,
    AccountRoutesMixin,
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
        if path == "/login":
            self._send_redirect("/login/")
            return
        if path == "/login/":
            self._handle_login_page()
            return
        if path == "/cycles":
            self._send_redirect("/cycles/")
            return
        if path == "/cycles/":
            self._handle_cycle_page()
            return
        if path == "/admin/users":
            self._send_redirect("/admin/users/")
            return
        if path == "/admin/users/":
            self._handle_users_page()
            return
        if path == "/api/v1/auth/session":
            principal = self._require_web_user()
            if principal is not None:
                self._handle_auth_session(principal)
            return
        if path == "/api/v1/admin/users":
            if self._require_admin() is not None:
                self._handle_admin_user_list()
            return
        report_page_match = re.fullmatch(r"/cycles/([^/]+)", path)
        if report_page_match:
            self._handle_report_page(report_page_match.group(1))
            return
        if path == "/api/v1/cycles":
            principal = self._require_any_scope("web_user", "operator")
            if principal is not None:
                self._handle_cycle_list(principal)
            return
        if path == "/api/v1/maintenance/cleanup":
            if self._require_admin() is not None:
                self._handle_cleanup_get()
            return
        analysis_match = re.fullmatch(r"/api/v1/cycles/([^/]+)/analysis", path)
        if analysis_match:
            if self._require_admin() is not None:
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
            principal = self._require_any_scope("web_user", "operator", "packaging")
            if principal is not None:
                self._handle_cycle_get(cycle_match.group(1), principal)
            return
        build_match = re.fullmatch(r"/api/v1/builds/([^/]+)", path)
        if build_match:
            if self._require_machine_scope("packaging") is not None:
                self._handle_build_get(build_match.group(1))
            return
        run_match = re.fullmatch(r"/api/v1/runs/([^/]+)", path)
        if run_match:
            if self._require_any_scope("operator") is not None:
                self._handle_run_get(run_match.group(1))
            return
        self._send_json(HTTPStatus.NOT_FOUND, {"error": "not_found"})
    def do_POST(self) -> None:
        """Routes authenticated probe, resource creation, and upload operations."""

        self._begin_request()
        path = urlsplit(self.path).path
        if path in ("/api/v1/auth/register", "/api/v1/auth/login"):
            if not self._require_same_origin_for_session_write():
                return
            if path.endswith("register"):
                self._handle_auth_register()
            else:
                self._handle_auth_login()
            return
        if path == "/api/v1/auth/logout":
            if self._require_web_user() is None or not self._require_same_origin_for_session_write():
                return
            self._handle_auth_logout()
            return
        if path == "/api/v1/admin/users":
            principal = self._require_admin()
            if principal is None:
                return
            if principal.kind == "web_user" and not self._require_same_origin_for_session_write():
                return
            self._handle_admin_user_create()
            return
        reset_match = re.fullmatch(r"/api/v1/admin/users/([^/]+)/reset-password", path)
        if reset_match:
            principal = self._require_admin()
            if principal is None:
                return
            if principal.kind == "web_user" and not self._require_same_origin_for_session_write():
                return
            self._handle_admin_user_reset_password(reset_match.group(1))
            return
        if path == "/api/v1/integrations/gitlab/merge-request-coverage/resolve":
            if not self._authorize_read():
                return
            self._handle_gitlab_merge_request_coverage_resolve()
            return
        retry_match = re.fullmatch(r"/api/v1/cycles/([^/]+)/analysis/retry", path)
        if retry_match:
            principal = self._require_admin()
            if principal is not None:
                if principal.kind != "web_user" or self._require_same_origin_for_session_write():
                    self._handle_analysis_retry(retry_match.group(1))
            return
        if path == "/api/v1/maintenance/cleanup":
            principal = self._require_admin()
            if principal is not None:
                if principal.kind != "web_user" or self._require_same_origin_for_session_write():
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
        if path == "/api/v1/probes":
            if self._require_machine_scope("run_upload") is not None:
                self._handle_probe_upload()
        elif path == "/api/v1/cycles":
            principal = self._require_any_scope("web_user", "operator")
            if principal is not None:
                if principal.kind != "web_user" or self._require_same_origin_for_session_write():
                    self._handle_cycle_create(principal)
        elif path == "/api/v1/builds":
            if self._require_machine_scope("packaging") is not None:
                self._handle_build_register()
        else:
            if self._require_machine_scope("run_upload") is not None:
                self._handle_run_upload()
    def do_PATCH(self) -> None:
        """Routes packaging Cycle head updates and administrator status changes."""

        self._begin_request()
        path = urlsplit(self.path).path
        status_match = re.fullmatch(r"/api/v1/admin/users/([^/]+)/status", path)
        if status_match:
            principal = self._require_admin()
            if principal is None:
                return
            if principal.kind == "web_user" and not self._require_same_origin_for_session_write():
                return
            self._handle_admin_user_status(status_match.group(1))
            return
        match = re.fullmatch(r"/api/v1/cycles/([^/]+)", path)
        if match is None:
            self._send_json(HTTPStatus.NOT_FOUND, {"error": "not_found"})
            return
        if self._require_machine_scope("packaging") is not None:
            self._handle_cycle_update(match.group(1))
