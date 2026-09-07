"""RunRoutesMixin for the stable HTTP boundary."""

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


class RunRoutesMixin:
    def _handle_run_upload(self) -> None:
        """Accepts one independently hashed profraw and returns its durable Run state."""

        try:
            text_fields, file_fields = self._read_multipart_body(
                {"metadata", "sha256"}, {"profile"},
                self.config.max_run_bytes + MULTIPART_OVERHEAD_BYTES,
            )
            metadata = self._decode_json_field(text_fields["metadata"], "metadata")
            profile = file_fields["profile"]
            run, duplicate = self.config.coverage_store.save_run(
                metadata,
                text_fields["sha256"],
                profile.filename or "",
                profile.file,
                self.config.max_run_bytes,
            )
        except (CoverageStoreError, ValueError) as error:
            self._send_domain_error(error)
            return
        status = HTTPStatus.OK if duplicate else (
            HTTPStatus.CREATED if run["analyzable"] else HTTPStatus.ACCEPTED
        )
        self._log_event(
            "run_replayed" if duplicate else "run_accepted",
            run_id=run["id"],
            cycle_id=run["cycle_id"],
            build_id=run["build_id"],
            file_size=run["payload_size"],
            sha256=run["payload_sha256"],
            run_state=run["state"],
            analyzable=run["analyzable"],
            idempotent_replay=duplicate,
        )
        if (
            not duplicate
            and run["state"] == "uploaded"
            and bool(run["analyzable"])
            and run["error_message"] in (None, "")
        ):
            self._request_analysis_safely(
                str(run["cycle_id"]), "run_uploaded", str(run["id"])
            )
        self._send_json(
            status,
            {
                "status": "accepted",
                "run_id": run["id"],
                "cycle_id": run["cycle_id"],
                "build_id": run["build_id"],
                "sha256": run["payload_sha256"],
                "size": run["payload_size"],
                "received_at": run["received_at"],
                "run_state": run["state"],
                "build_registered": bool(run["analyzable"]),
                "analyzable": bool(run["analyzable"]),
                "idempotent_replay": duplicate,
            },
        )
    def _handle_run_get(self, run_id: str) -> None:
        """Returns one stored Run state without exposing local paths."""

        try:
            run = self.config.coverage_store.get_run(run_id)
        except CoverageStoreError as error:
            self._send_domain_error(error)
            return
        self._send_json(HTTPStatus.OK, {"run": run})
