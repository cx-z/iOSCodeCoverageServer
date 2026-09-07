"""RequestMixin for the stable HTTP boundary."""

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


class RequestMixin:
    def _read_json_body(self) -> Dict[str, object]:
        """Reads one small JSON object with an explicit content type and length."""

        content_type = self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
        if content_type != "application/json":
            raise CoverageStoreError("json_required", "Content-Type must be application/json")
        content_length = self._parse_content_length()
        if content_length is None:
            raise CoverageStoreError("content_length_required", "Content-Length is required")
        if content_length <= 0 or content_length > MAX_JSON_BYTES:
            raise CoverageStoreError("request_too_large", "JSON request exceeds 64 KiB")
        body = self.rfile.read(content_length)
        if len(body) != content_length:
            raise CoverageStoreError("incomplete_request", "request body ended early")
        return self._decode_json_field(body.decode("utf-8"), "request")
    def _decode_json_field(self, value: str, field_name: str) -> Dict[str, object]:
        """Decodes one JSON object and rejects arrays or scalar payloads."""

        try:
            payload = json.loads(value)
        except (TypeError, ValueError, UnicodeError):
            raise CoverageStoreError("invalid_json", field_name + " must be valid UTF-8 JSON")
        if not isinstance(payload, dict):
            raise CoverageStoreError("invalid_json", field_name + " must be a JSON object")
        return payload
    def _parse_content_length(self) -> Optional[int]:
        """Parses a non-negative Content-Length header without accepting junk."""

        raw_value = self.headers.get("Content-Length")
        if raw_value is None:
            return None
        try:
            return int(raw_value, 10)
        except ValueError:
            return None
