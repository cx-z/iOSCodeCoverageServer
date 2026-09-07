"""BuildRoutesMixin for the stable HTTP boundary."""

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


class BuildRoutesMixin:
    def _handle_build_register(self) -> None:
        """Registers one exact required/optional Build artifact set."""

        try:
            text_fields, file_fields = self._read_multipart_body(
                {"manifest"}, {"main_executable", "dsym"},
                self.config.max_build_artifact_bytes * 3 + MULTIPART_OVERHEAD_BYTES,
                optional_file_fields={"debug_dylib"},
            )
            manifest = self._decode_json_field(text_fields["manifest"], "manifest")
            artifacts = {
                name: (part.filename or "", part.file) for name, part in file_fields.items()
            }
            build, duplicate = self.config.coverage_store.register_build(
                manifest, artifacts, self.config.max_build_artifact_bytes
            )
        except (CoverageStoreError, ValueError) as error:
            self._send_domain_error(error)
            return
        self._log_event(
            "build_registered" if not duplicate else "build_replayed",
            build_id=build["id"],
            cycle_id=build["cycle_id"],
            idempotent_replay=duplicate,
        )
        if not duplicate and int(build["pending_runs_promoted"]) > 0:
            self._request_analysis_safely(
                str(build["cycle_id"]), "pending_runs_promoted"
            )
        self._send_json(
            HTTPStatus.OK if duplicate else HTTPStatus.CREATED,
            {
                "status": "registered",
                "build_id": build["id"],
                "cycle_id": build["cycle_id"],
                "artifact_state": build["artifact_state"],
                "pending_runs_promoted": build["pending_runs_promoted"],
                "idempotent_replay": duplicate,
            },
        )
    def _handle_build_get(self, build_id: str) -> None:
        """Returns persisted Build identity, toolchain, UUID, and hash metadata."""

        try:
            build = self.config.coverage_store.get_build(build_id)
        except CoverageStoreError as error:
            self._send_domain_error(error)
            return
        self._send_json(HTTPStatus.OK, {"build": build})
