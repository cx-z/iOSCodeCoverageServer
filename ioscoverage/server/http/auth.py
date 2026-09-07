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


class AuthenticationMixin:
    def _require_token(self, allow_run_upload: bool = False) -> bool:
        """Allows operator credentials everywhere and the iOS credential only when scoped."""

        credential_scope = self._credential_scope()
        if credential_scope == "operator" or (
            credential_scope == "run_upload" and allow_run_upload
        ):
            return True
        if credential_scope == "run_upload":
            self._log_event("authorization_denied", level=logging.WARNING, reason="insufficient_scope")
            self._send_json(
                HTTPStatus.FORBIDDEN,
                {"error": "insufficient_scope", "message": "token is not allowed for this endpoint"},
            )
            return False
        self._log_event("authentication_failed", level=logging.WARNING)
        self._send_json(
            HTTPStatus.UNAUTHORIZED,
            {"error": "invalid_token"},
            extra_headers={"WWW-Authenticate": "Bearer"},
        )
        return False
    def _has_valid_token(self) -> bool:
        """Retains the operator-token predicate used by existing component boundaries."""

        return self._credential_scope() == "operator"
    def _credential_scope(self) -> Optional[str]:
        """Classifies one Bearer credential without recording its secret value."""

        authorization = self.headers.get("Authorization", "")
        prefix = "Bearer "
        if not authorization.startswith(prefix):
            return None
        supplied = authorization[len(prefix) :]
        if secrets.compare_digest(supplied, self.config.token):
            return "operator"
        run_upload_token = self.config.run_upload_token
        if run_upload_token is not None and secrets.compare_digest(supplied, run_upload_token):
            return "run_upload"
        return None
