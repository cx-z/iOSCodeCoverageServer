"""CycleRoutesMixin for the stable HTTP boundary."""

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


class CycleRoutesMixin:
    def _handle_cycle_create(self, principal) -> None:
        """Creates operator Cycles or one web user's MR submission relationship."""

        try:
            payload = self._read_json_body()
            if set(payload) == {"schema_version", "mr_url"}:
                if payload.get("schema_version") != 1 or isinstance(
                    payload.get("schema_version"), bool
                ):
                    raise CoverageStoreError(
                        "invalid_schema_version", "schema_version must be 1"
                    )
                client = self.config.gitlab_mr_client
                if client is None:
                    raise GitLabMRClientError(
                        "gitlab_client_unavailable",
                        "GitLab MR lookup is unavailable",
                        HTTPStatus.SERVICE_UNAVAILABLE,
                    )
                merge_request = client.fetch(payload.get("mr_url"))
                cycle_payload = {
                        "schema_version": 1,
                        "project": merge_request.project,
                        "mr_iid": str(merge_request.iid),
                        "mr_url": merge_request.web_url,
                        "target_branch": merge_request.target_branch,
                        "base_sha": merge_request.base_sha,
                        "head_sha": merge_request.head_sha,
                    }
                if principal.kind == "web_user":
                    cycle, reused, relationship_existed = (
                        self.config.coverage_store.create_or_get_cycle_for_user(
                            cycle_payload, str(principal.user_id)
                        )
                    )
                else:
                    cycle, reused = self.config.coverage_store.create_or_get_cycle(cycle_payload)
                    relationship_existed = False
            else:
                if principal.kind == "web_user":
                    raise CoverageStoreError(
                        "invalid_fields", "web users may submit only schema_version and mr_url"
                    )
                cycle = self.config.coverage_store.create_cycle(payload)
                reused = False
                relationship_existed = False
        except GitLabMRClientError as error:
            self._send_gitlab_error(error)
            return
        except (CoverageStoreError, ValueError) as error:
            self._send_domain_error(error)
            return
        event = "cycle_reused" if reused else "cycle_created"
        self._log_event(event, cycle_id=cycle["id"], mr_iid=cycle.get("mr_iid"))
        response = {"status": "reused" if reused else "created", "cycle": cycle}
        if principal.kind == "web_user":
            response["submission_status"] = (
                "existing" if relationship_existed else "added"
            )
        self._send_json(HTTPStatus.OK if reused else HTTPStatus.CREATED, response)
    def _handle_cycle_list(self, principal) -> None:
        """Returns the principal-scoped Cycle projection newest first."""

        try:
            if principal.kind == "web_user" and principal.role == "developer":
                cycles = self.config.coverage_store.list_cycles_for_user(
                    str(principal.user_id)
                )
            elif principal.kind == "web_user" and principal.role == "admin":
                cycles = self.config.coverage_store.list_cycles_with_submitters()
            else:
                cycles = self.config.coverage_store.list_cycles()
        except CoverageStoreError as error:
            self._send_domain_error(error)
            return
        self._send_json(HTTPStatus.OK, {"cycles": cycles, "count": len(cycles)})
    def _handle_cycle_get(self, cycle_id: str, principal) -> None:
        """Returns one known Cycle while preventing developer ownership bypass."""

        try:
            if (
                principal.kind == "web_user"
                and principal.role == "developer"
                and not self.config.coverage_store.user_submitted_cycle(
                    str(principal.user_id), cycle_id
                )
            ):
                raise CoverageStoreError("cycle_not_found", "cycle_id is not registered")
            cycle = self.config.coverage_store.get_cycle(cycle_id)
        except CoverageStoreError as error:
            self._send_domain_error(error)
            return
        self._send_json(HTTPStatus.OK, {"cycle": cycle})
    def _handle_cycle_update(self, cycle_id: str) -> None:
        """Updates only head_sha and rejects any attempt to mutate base_sha."""

        try:
            payload = self._read_json_body()
            cycle = self.config.coverage_store.update_cycle_head(cycle_id, payload)
        except (CoverageStoreError, ValueError) as error:
            self._send_domain_error(error)
            return
        self._log_event("cycle_head_updated", cycle_id=cycle_id, head_sha=cycle["head_sha"])
        self._send_json(HTTPStatus.OK, {"status": "updated", "cycle": cycle})
