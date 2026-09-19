"""AuthenticationMixin for the stable HTTP boundary."""

import cgi
import hashlib
import html
import json
import logging
import os
import re
import secrets
import tempfile
import time
import traceback
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from email import policy
from email.parser import BytesParser
from http.cookies import SimpleCookie
from http import HTTPStatus
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from urllib.parse import urlsplit

from ioscoverage.analysis.scheduler import AnalysisSchedulerError
from ioscoverage.integrations.gitlab_mr import GitLabMRClientError
from ioscoverage.integrations.xcode_artifacts import ArtifactValidationError
from ioscoverage.storage.analysis_state import AnalysisStateStoreError
from ioscoverage.storage.coverage_store import CoverageStoreError
from ioscoverage.storage.reports import CoverageReportError
from ioscoverage.server.config import *
from ioscoverage.web.pages.cycles import render_cycle_list_page
from ioscoverage.web.pages.report import render_coverage_report_page


SESSION_COOKIE_NAME = "ios_coverage_session"


@dataclass(frozen=True)
class RequestPrincipal:
    """Represents one request-confined machine, web-user, or anonymous identity."""

    kind: str
    user_id: Optional[str] = None
    username: Optional[str] = None
    role: Optional[str] = None


class AuthenticationMixin:
    def _require_token(self, allow_run_upload: bool = False) -> bool:
        """Preserves the operator/run-upload helper while using unified principals."""

        scopes = ("operator", "run_upload") if allow_run_upload else ("operator",)
        return self._require_any_scope(*scopes) is not None

    def _has_valid_token(self) -> bool:
        """Retains the operator-token predicate used by existing component boundaries."""

        principal = self._resolve_principal()
        return principal is not None and principal.kind == "operator"

    def _authorize_read(self) -> bool:
        """Allows fixed read-only endpoints anonymously or to any authenticated principal.

        The read-only routes never create, modify, or delete data; the trusted-intranet
        boundary is owned by the listen address and network policy. An explicitly invalid
        Bearer credential always fails rather than falling back to Cookie or anonymous access.
        """

        principal = self._resolve_principal()
        if principal is None:
            self._send_authentication_failure()
            return False
        if self.config.anonymous_read or principal.kind != "anonymous":
            return True
        self._send_authentication_failure()
        return False

    def _credential_scope(self) -> Optional[str]:
        """Returns the machine scope for compatibility with existing component tests."""

        principal = self._resolve_principal()
        if principal is None or principal.kind not in ("operator", "packaging", "run_upload"):
            return None
        return principal.kind

    def _resolve_principal(self) -> Optional[RequestPrincipal]:
        """Resolves Bearer before Cookie and caches only immutable request-local identity."""

        if bool(getattr(self, "_request_principal_resolved", False)):
            return self._request_principal
        authorization = self.headers.get("Authorization")
        if authorization is not None:
            supplied = authorization[len("Bearer ") :] if authorization.startswith("Bearer ") else ""
            candidates = (
                ("operator", self.config.token),
                ("packaging", self.config.packaging_token),
                ("run_upload", self.config.run_upload_token),
            )
            for kind, expected in candidates:
                if expected is not None and supplied and secrets.compare_digest(supplied, expected):
                    self._request_principal = RequestPrincipal(kind)
                    self._request_principal_resolved = True
                    self._invalid_bearer = False
                    return self._request_principal
            self._request_principal = None
            self._request_principal_resolved = True
            self._invalid_bearer = True
            return None
        self._invalid_bearer = False
        token = self._session_cookie_token()
        account_store = self.config.account_store
        user = account_store.resolve_session(token) if account_store is not None and token else None
        if user is None:
            self._request_principal = RequestPrincipal("anonymous")
        else:
            self._request_principal = RequestPrincipal(
                "web_user",
                user_id=str(user["id"]),
                username=str(user["username"]),
                role=str(user["role"]),
            )
        self._request_principal_resolved = True
        return self._request_principal

    def _require_any_scope(self, *scopes: str) -> Optional[RequestPrincipal]:
        """Returns an allowed principal or emits one stable 401/403 response."""

        principal = self._resolve_principal()
        if principal is None or principal.kind == "anonymous":
            self._send_authentication_failure()
            return None
        if principal.kind in scopes:
            return principal
        self._log_event("authorization_denied", level=logging.WARNING, reason="insufficient_scope")
        self._send_json(
            HTTPStatus.FORBIDDEN,
            {"error": "insufficient_scope", "message": "identity is not allowed for this endpoint"},
        )
        return None

    def _require_web_user(self) -> Optional[RequestPrincipal]:
        """Requires one active developer or administrator browser session."""

        return self._require_any_scope("web_user")

    def _require_admin(self) -> Optional[RequestPrincipal]:
        """Allows an admin browser session or the compatibility operator credential."""

        principal = self._resolve_principal()
        if principal is None or principal.kind == "anonymous":
            self._send_authentication_failure()
            return None
        if principal.kind == "operator" or (
            principal.kind == "web_user" and principal.role == "admin"
        ):
            return principal
        self._log_event("authorization_denied", level=logging.WARNING, reason="insufficient_scope")
        self._send_json(
            HTTPStatus.FORBIDDEN,
            {"error": "insufficient_scope", "message": "administrator access is required"},
        )
        return None

    def _require_machine_scope(self, scope: str) -> Optional[RequestPrincipal]:
        """Allows the requested machine scope while retaining operator compatibility."""

        return self._require_any_scope("operator", scope)

    def _require_same_origin_for_session_write(self) -> bool:
        """Requires a JSON browser write Origin to match the current Host exactly."""

        origin = self.headers.get("Origin", "")
        host = self.headers.get("Host", "")
        parsed = urlsplit(origin)
        if (
            parsed.scheme not in ("http", "https")
            or not host
            or parsed.netloc != host
            or parsed.path not in ("", "/")
            or parsed.query
            or parsed.fragment
        ):
            self._send_json(
                HTTPStatus.FORBIDDEN,
                {"error": "origin_not_allowed", "message": "request Origin must match Host"},
            )
            return False
        return True

    def _session_cookie_token(self) -> Optional[str]:
        """Extracts the fixed Cookie value without logging or persisting its raw form."""

        raw_cookie = self.headers.get("Cookie", "")
        if not raw_cookie:
            return None
        cookie = SimpleCookie()
        try:
            cookie.load(raw_cookie)
        except Exception:
            return None
        morsel = cookie.get(SESSION_COOKIE_NAME)
        return None if morsel is None else morsel.value

    def _send_authentication_failure(self) -> None:
        """Distinguishes an explicitly invalid Bearer from a missing/invalid web session."""

        invalid_bearer = bool(getattr(self, "_invalid_bearer", False))
        code = "invalid_token" if invalid_bearer else "authentication_required"
        self._log_event("authentication_failed", level=logging.WARNING, reason=code)
        self._send_json(
            HTTPStatus.UNAUTHORIZED,
            {"error": code, "message": "authentication is required"},
            extra_headers={"WWW-Authenticate": "Bearer"},
        )
