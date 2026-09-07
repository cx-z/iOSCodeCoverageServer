"""MultipartMixin for the stable HTTP boundary."""

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


class MultipartMixin:
    def _read_multipart_body(
        self,
        expected_text_fields: set,
        expected_file_fields: set,
        maximum_request_bytes: int,
        optional_file_fields: Optional[set] = None,
    ) -> Tuple[Dict[str, str], Dict[str, cgi.FieldStorage]]:
        """Spools a bounded multipart body and enforces required and optional fields."""

        content_type = self.headers.get("Content-Type", "")
        if not content_type.lower().startswith("multipart/form-data"):
            raise CoverageStoreError(
                "multipart_form_data_required", "Content-Type must be multipart/form-data"
            )
        content_length = self._parse_content_length()
        if content_length is None:
            raise CoverageStoreError("content_length_required", "Content-Length is required")
        if content_length <= 0 or content_length > maximum_request_bytes:
            raise CoverageStoreError("request_too_large", "multipart request exceeds its configured limit")
        form = cgi.FieldStorage(
            fp=self.rfile,
            headers=self.headers,
            environ={
                "REQUEST_METHOD": "POST",
                "CONTENT_TYPE": content_type,
                "CONTENT_LENGTH": str(content_length),
            },
            keep_blank_values=True,
        )
        text_fields: Dict[str, str] = {}
        file_fields: Dict[str, cgi.FieldStorage] = {}
        for part in form.list or []:
            name = part.name or ""
            if name in text_fields or name in file_fields:
                raise CoverageStoreError("duplicate_multipart_field", "duplicate field: " + name)
            if part.filename is None:
                raw_value = part.value
                value = raw_value if isinstance(raw_value, str) else raw_value.decode("utf-8")
                if len(value.encode("utf-8")) > MAX_JSON_BYTES:
                    raise CoverageStoreError("field_too_large", name + " exceeds 64 KiB")
                text_fields[name] = value.strip()
            else:
                part.file.seek(0)
                file_fields[name] = part
        allowed_file_fields = expected_file_fields | (optional_file_fields or set())
        if (
            set(text_fields) != expected_text_fields
            or not expected_file_fields.issubset(set(file_fields))
            or not set(file_fields).issubset(allowed_file_fields)
        ):
            raise CoverageStoreError(
                "invalid_multipart_fields",
                "multipart fields do not match the endpoint contract",
            )
        return text_fields, file_fields
    def _parse_multipart(
        self, content_type: str, body: bytes
    ) -> Tuple[Dict[str, str], str, bytes]:
        """Extracts the sha256 field and one file from a bounded multipart body."""

        envelope = (
            "Content-Type: " + content_type + "\r\nMIME-Version: 1.0\r\n\r\n"
        ).encode("utf-8") + body
        message = BytesParser(policy=policy.default).parsebytes(envelope)
        if not message.is_multipart():
            raise ValueError("invalid_multipart_body")

        fields: Dict[str, str] = {}
        filename = ""
        payload: Optional[bytes] = None
        for part in message.iter_parts():
            field_name = part.get_param("name", header="content-disposition")
            if field_name == "file":
                if payload is not None:
                    raise ValueError("exactly_one_file_required")
                filename = part.get_filename() or "upload.bin"
                payload = part.get_payload(decode=True)
            elif field_name:
                decoded = part.get_payload(decode=True)
                fields[field_name] = (decoded or b"").decode("utf-8", errors="strict").strip()

        if payload is None:
            raise ValueError("file_field_required")
        return fields, filename, payload
