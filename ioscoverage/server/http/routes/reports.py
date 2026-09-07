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


class ReportRoutesMixin:
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
