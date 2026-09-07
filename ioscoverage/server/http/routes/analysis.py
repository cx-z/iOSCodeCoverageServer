"""AnalysisRoutesMixin for the stable HTTP boundary."""

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


class AnalysisRoutesMixin:
    def _handle_analysis_get(self, cycle_id: str) -> None:
        """Returns bounded Cycle analysis state while retaining prior success."""

        try:
            state = self.config.analysis_state_store.get_state(cycle_id)
        except AnalysisStateStoreError as error:
            self._send_analysis_error(error)
            return
        self._send_json(HTTPStatus.OK, self._analysis_api_document(state))
    def _handle_analysis_retry(self, cycle_id: str) -> None:
        """Queues one manual generation without accepting commands or input overrides."""

        content_length = self._parse_content_length()
        if content_length not in (None, 0):
            self._send_json(
                HTTPStatus.BAD_REQUEST,
                {"error": "request_body_not_allowed", "message": "retry accepts no request body"},
            )
            return
        scheduler = self.config.analysis_scheduler
        if scheduler is None:
            self._send_json(
                HTTPStatus.SERVICE_UNAVAILABLE,
                {"error": "analysis_scheduler_unavailable"},
            )
            return
        try:
            state = scheduler.request_analysis(cycle_id, "manual_retry")
        except (AnalysisStateStoreError, AnalysisSchedulerError) as error:
            self._send_analysis_error(error)
            return
        self._log_event(
            "analysis_retry_requested",
            cycle_id=state["cycle_id"],
            requested_generation=state["requested_generation"],
            analysis_status=state["status"],
        )
        self._send_json(HTTPStatus.ACCEPTED, self._analysis_api_document(state))
    def _request_analysis_safely(
        self, cycle_id: str, trigger: str, latest_run_id: Optional[str] = None
    ) -> None:
        """Requests background work without changing a committed Build/Run response."""

        scheduler = self.config.analysis_scheduler
        if scheduler is None:
            return
        try:
            state = scheduler.request_analysis(cycle_id, trigger, latest_run_id)
        except (AnalysisStateStoreError, AnalysisSchedulerError) as error:
            self._log_event(
                "analysis_request_failed",
                level=logging.ERROR,
                cycle_id=cycle_id,
                trigger=trigger,
                reason=error.code,
            )
            return
        self._log_event(
            "analysis_requested",
            cycle_id=cycle_id,
            trigger=trigger,
            requested_generation=state["requested_generation"],
            analysis_status=state["status"],
        )
    def _analysis_api_document(self, state: Dict[str, object]) -> Dict[str, object]:
        """Projects internal persistence without local paths, commands, or credentials."""

        latest_success = None
        if state["latest_success_generation"] is not None:
            latest_success = {
                "generation": state["latest_success_generation"],
                "current_build_id": state["latest_success_current_build_id"],
                "orchestration_input_sha256": state[
                    "latest_success_orchestration_sha256"
                ],
                "cycle_input_set_sha256": state[
                    "latest_success_cycle_input_sha256"
                ],
                "summary": state["latest_success_summary"],
            }
        error = None
        if state["error_code"] is not None:
            error = {
                "stage": state["error_stage"],
                "code": state["error_code"],
                "message": state["error_message"],
            }
        return {
            "cycle_id": state["cycle_id"],
            "status": state["status"],
            "requested_generation": state["requested_generation"],
            "completed_generation": state["completed_generation"],
            "attempt_count": state["attempt_count"],
            "requested_at": state["requested_at"],
            "started_at": state["started_at"],
            "completed_at": state["completed_at"],
            "last_trigger": state["last_trigger"],
            "latest_run_id": state["latest_run_id"],
            "current_build_id": state["current_build_id"],
            "orchestration_input_sha256": state["orchestration_input_sha256"],
            "cycle_input_set_sha256": state["cycle_input_set_sha256"],
            "latest_success": latest_success,
            "error": error,
        }
