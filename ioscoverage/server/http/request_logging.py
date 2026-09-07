"""RequestLoggingMixin for the stable HTTP boundary."""

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
from ioscoverage.server.logging import write_log
from ioscoverage.web.pages.cycles import render_cycle_list_page
from ioscoverage.web.pages.report import render_coverage_report_page


class RequestLoggingMixin:
    def _begin_request(self) -> None:
        """Initializes per-request diagnostic context owned by this handler instance."""

        self.request_id = uuid.uuid4().hex[:12]
        self.request_started_at = time.monotonic()
        self._log_event(
            "request_started",
            content_length=self._parse_content_length(),
            user_agent=self.headers.get("User-Agent", "")[:512],
        )
    def _request_id(self) -> str:
        """Returns a stable ID even for protocol errors raised before routing."""

        request_id = getattr(self, "request_id", None)
        if request_id is None:
            request_id = uuid.uuid4().hex[:12]
            self.request_id = request_id
        return request_id
    def _log_event(self, event: str, level: int = logging.INFO, **fields: object) -> None:
        """Appends one structured event without logging credentials or request bodies."""

        write_log(
            self.config.logger,
            event,
            level=level,
            request_id=self._request_id(),
            client_ip=self.client_address[0],
            client_port=self.client_address[1],
            method=self.command,
            path=urlsplit(self.path).path,
            **fields,
        )
    def log_message(self, format_string: str, *arguments: object) -> None:
        """Redirects standard HTTP diagnostics into the persistent structured log."""

        self._log_event("http_server_message", message=format_string % arguments)
