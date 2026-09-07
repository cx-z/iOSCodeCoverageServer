"""ResponseMixin for the stable HTTP boundary."""

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
from datetime import datetime, timezone
from email import policy
from email.parser import BytesParser
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


class ResponseMixin:
    def _send_json(
        self,
        status: HTTPStatus,
        payload: Dict[str, object],
        extra_headers: Optional[Dict[str, str]] = None,
    ) -> None:
        """Sends a UTF-8 JSON response with conservative browser headers."""

        body = (json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8")
        self._send_response(status, "application/json; charset=utf-8", body, extra_headers)
    def _send_html(self, status: HTTPStatus, body: bytes) -> None:
        """Sends the local diagnostic HTML page."""

        self._send_response(
            status,
            "text/html; charset=utf-8",
            body,
            {
                "Content-Security-Policy": (
                    "default-src 'self'; script-src 'unsafe-inline'; "
                    "style-src 'unsafe-inline'; connect-src 'self'; img-src 'none'; "
                    "object-src 'none'; base-uri 'none'; frame-ancestors 'none'"
                ),
                "Referrer-Policy": "no-referrer",
                "X-Frame-Options": "DENY",
            },
        )
    def _send_redirect(self, location: str) -> None:
        """Redirects one fixed local route without reflecting request-controlled input."""

        self._send_response(
            HTTPStatus.PERMANENT_REDIRECT,
            "text/plain; charset=utf-8",
            b"Redirecting to /cycles/\n",
            {"Location": location},
        )
    def _send_response(
        self,
        status: HTTPStatus,
        content_type: str,
        body: bytes,
        extra_headers: Optional[Dict[str, str]] = None,
    ) -> None:
        """Writes common response headers without exposing local paths or secrets."""

        self.send_response(status.value)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        if extra_headers:
            for name, value in extra_headers.items():
                self.send_header(name, value)
        self.send_header("X-Request-ID", self._request_id())
        self.end_headers()
        self.wfile.write(body)
        started_at = getattr(self, "request_started_at", None)
        duration_ms = None
        if started_at is not None:
            duration_ms = round((time.monotonic() - started_at) * 1000, 2)
        self._log_event(
            "request_completed",
            status=status.value,
            response_bytes=len(body),
            duration_ms=duration_ms,
        )
