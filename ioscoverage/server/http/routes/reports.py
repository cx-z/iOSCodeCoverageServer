"""ReportRoutesMixin for the stable HTTP boundary."""

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
from urllib.parse import parse_qs, urlsplit

from ioscoverage.analysis.scheduler import AnalysisSchedulerError
from ioscoverage.integrations.gitlab_mr import GitLabMRClientError
from ioscoverage.integrations.xcode_artifacts import ArtifactValidationError
from ioscoverage.storage.analysis_state import AnalysisStateStoreError
from ioscoverage.storage.coverage_store import CoverageStoreError
from ioscoverage.storage.reports import CoverageReportError
from ioscoverage.server.config import *
from ioscoverage.web.pages.cycles import render_cycle_list_page
from ioscoverage.web.pages.report import render_coverage_report_page


class ReportRoutesMixin:
    def _handle_cycle_presentation_get(self, cycle_id: str) -> None:
        """Reads latest MR summary or an issued immutable detail under the report boundary."""

        resolver = self.config.merge_request_coverage_resolver
        if resolver is None:
            self._send_json(HTTPStatus.SERVICE_UNAVAILABLE, {"error": "merge_request_coverage_resolver_unavailable"})
            return
        try:
            query = parse_qs(urlsplit(self.path).query, keep_blank_values=True, max_num_fields=3)
            if query and (set(query) != {"page_version", "presentation_id", "file_id"}
                          or any(len(values) != 1 for values in query.values())):
                raise CoverageStoreError("invalid_fields", "MR detail identity is invalid")
            params = {key: values[0] for key, values in query.items()}
            if params and (re.fullmatch(r"[1-9][0-9]{0,18}", params["page_version"]) is None
                           or any(re.fullmatch(r"[0-9a-f]{64}", params[key]) is None
                                  for key in ("presentation_id", "file_id"))):
                raise CoverageStoreError("invalid_fields", "MR detail identity is invalid")
            document = resolver.resolve_cycle_presentation(cycle_id, **params)
        except GitLabMRClientError as error:
            self._send_gitlab_error(error)
            return
        except CoverageReportError as error:
            self._send_report_error(error)
            return
        except (CoverageStoreError, ValueError) as error:
            self._send_domain_error(error)
            return
        self._send_json(HTTPStatus.OK, document)

    def _handle_report_page(self, cycle_id: str) -> None:
        """Returns a data-free report shell; JSON data remains Bearer protected."""

        try:
            body = render_coverage_report_page(cycle_id)
        except (ValueError, AttributeError):
            self._send_json(
                HTTPStatus.BAD_REQUEST,
                {"error": "invalid_uuid", "message": "cycle_id must be canonical"},
            )
            return
        self._send_html(HTTPStatus.OK, body)
    def _handle_report_get(self, cycle_id: str) -> None:
        """Returns latest summary and server-issued file IDs without local paths."""

        report_store = self.config.report_store
        if report_store is None:
            self._send_json(
                HTTPStatus.SERVICE_UNAVAILABLE, {"error": "report_store_unavailable"}
            )
            return
        try:
            document = report_store.get_report(cycle_id)
        except CoverageReportError as error:
            self._send_report_error(error)
            return
        self._send_json(HTTPStatus.OK, document)
    def _handle_report_file_get(self, cycle_id: str, file_id: str) -> None:
        """Returns changed lines selected by an opaque file ID, never a path."""

        report_store = self.config.report_store
        if report_store is None:
            self._send_json(
                HTTPStatus.SERVICE_UNAVAILABLE, {"error": "report_store_unavailable"}
            )
            return
        try:
            document = report_store.get_file(cycle_id, file_id)
        except CoverageReportError as error:
            self._send_report_error(error)
            return
        self._send_json(HTTPStatus.OK, document)
