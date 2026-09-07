"""ErrorResponseMixin for the stable HTTP boundary."""

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


class ErrorResponseMixin:
    def _send_report_error(self, error: CoverageReportError) -> None:
        """Maps bounded report failures without exposing filesystem evidence."""

        if error.code in ("cycle_not_found", "report_file_not_found"):
            status = HTTPStatus.NOT_FOUND
        elif error.code in ("invalid_uuid",):
            status = HTTPStatus.BAD_REQUEST
        elif error.code == "report_not_available":
            status = HTTPStatus.CONFLICT
        else:
            status = HTTPStatus.INTERNAL_SERVER_ERROR
        self._log_event("report_request_rejected", level=logging.WARNING, reason=error.code)
        self._send_json(status, {"error": error.code, "message": error.message})
    def _send_analysis_error(self, error: Exception) -> None:
        """Maps state/scheduler errors without exposing internal exception details."""

        code = getattr(error, "code", "analysis_state_error")
        message = getattr(error, "message", "analysis state request failed")
        status = HTTPStatus.NOT_FOUND if code == "cycle_not_found" else (
            HTTPStatus.SERVICE_UNAVAILABLE
            if code == "analysis_scheduler_stopping"
            else HTTPStatus.BAD_REQUEST
        )
        self._log_event("analysis_request_rejected", level=logging.WARNING, reason=code)
        self._send_json(status, {"error": code, "message": message})
    def _send_gitlab_error(self, error: GitLabMRClientError) -> None:
        """Returns a stable MR lookup error without exposing credentials or raw upstream data."""

        try:
            status = HTTPStatus(error.http_status)
        except ValueError:
            status = HTTPStatus.BAD_GATEWAY
        self._log_event("gitlab_mr_lookup_rejected", level=logging.WARNING, reason=error.code)
        self._send_json(status, {"error": error.code, "message": error.message})
    def _send_domain_error(self, error: Exception) -> None:
        """Maps stable persistence/validation failures to bounded JSON API errors."""

        if isinstance(error, CoverageStoreError):
            code = error.code
            message = error.message
        else:
            code = "invalid_request"
            message = str(error)
        if code in (
            "git_object_not_found",
            "merge_base_unavailable",
            "merge_base_mismatch",
            "artifact_uuid_unreadable",
            "artifact_uuid_mismatch",
            "coverage_binary_dsym_uuid_mismatch",
            "dsym_uuid_unreadable",
            "invalid_dsym_zip",
            "unsafe_dsym_zip",
        ):
            status = HTTPStatus.UNPROCESSABLE_ENTITY
        elif code in ("git_repository_unavailable", "artifact_tool_unavailable"):
            status = HTTPStatus.SERVICE_UNAVAILABLE
        elif code.endswith("_not_found"):
            status = HTTPStatus.NOT_FOUND
        elif code in ("run_id_conflict", "build_id_conflict", "storage_conflict"):
            status = HTTPStatus.CONFLICT
        elif code in (
            "request_too_large",
            "file_too_large",
            "field_too_large",
            "dsym_archive_too_large",
        ):
            status = HTTPStatus.REQUEST_ENTITY_TOO_LARGE
        elif code in ("sha256_mismatch", "size_mismatch"):
            status = HTTPStatus.UNPROCESSABLE_ENTITY
        elif code == "content_length_required":
            status = HTTPStatus.LENGTH_REQUIRED
        elif code == "multipart_form_data_required":
            status = HTTPStatus.UNSUPPORTED_MEDIA_TYPE
        elif code == "stored_file_missing":
            status = HTTPStatus.INTERNAL_SERVER_ERROR
        else:
            status = HTTPStatus.BAD_REQUEST
        self._log_event("request_rejected", level=logging.WARNING, reason=code)
        self._send_json(status, {"error": code, "message": message})
