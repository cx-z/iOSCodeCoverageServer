"""Read-only GitLab MR, Cycle, Build, and report identity resolver."""

import re
import hashlib
import json
import threading
import time
import copy
from concurrent.futures import Future
from collections import OrderedDict

from ioscoverage.analysis.mr_presentation import MergeRequestPresentation, LOCAL_ERRORS
from typing import Dict, Mapping, Optional

from ioscoverage.integrations.gitlab_mr import (
    GitLabMRClient,
    GitLabMRClientError,
    GitLabMergeRequest,
    NormalizedMRURL,
    normalize_mr_url,
)
from ioscoverage.storage.analysis_state import AnalysisStateStore, AnalysisStateStoreError
from ioscoverage.storage.coverage_store import CoverageStore, CoverageStoreError
from ioscoverage.storage.reports import CoverageReportError, CoverageReportStore


GIT_SHA_PATTERN = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
PRODUCT_GITLAB_STATUSES = {"mr_not_open", "mr_diff_refs_not_ready"}
PENDING_ANALYSIS_STATUSES = {"queued", "running"}
SUMMARY_FIELDS = (
    "changed_new_side",
    "measurable",
    "covered",
    "uncovered",
    "not_measurable",
    "unknown",
    "changed_line_coverage_percent",
)


class MergeRequestCoverageResolver:
    """Combines existing read-only identities without scheduling or persistence writes."""

    def __init__(
        self,
        gitlab_client: GitLabMRClient,
        coverage_store: CoverageStore,
        analysis_state_store: AnalysisStateStore,
        report_store: CoverageReportStore,
    ):
        """Captures startup-fixed collaborators and accepts no request-controlled paths."""

        self.gitlab_client = gitlab_client
        self.coverage_store = coverage_store
        self.analysis_state_store = analysis_state_store
        self.report_store = report_store
        # Cache owns immutable derived snapshots, not mutable Build/Run or Git state.
        self._presentations = OrderedDict()
        self._presentation_inputs = {}
        self._projection_pending = {}
        self._presentation_lock = threading.Lock()
        self._projection_slots = threading.BoundedSemaphore(2)

    def resolve(self, mr_url: object) -> Dict[str, object]:
        """Resolves one registered MR to a completion-verified annotation decision."""

        normalized = normalize_mr_url(mr_url, self.gitlab_client.project_registry)
        try:
            merge_request = self.gitlab_client.fetch(mr_url)
        except GitLabMRClientError as error:
            if error.code not in PRODUCT_GITLAB_STATUSES:
                raise
            return self._document(
                error.code,
                False,
                False,
                self._normalized_mr_document(normalized),
                None,
                None,
                None,
            )

        mr_document = self._mr_document(merge_request)
        cycle = self.coverage_store.find_cycle_by_merge_request(
            merge_request.project, merge_request.iid
        )
        if cycle is None:
            return self._document(
                "cycle_not_found", False, False, mr_document, None, None, None
            )

        cycle_id = str(cycle["id"])
        try:
            state = self.analysis_state_store.get_state(cycle_id)
        except AnalysisStateStoreError as error:
            raise CoverageReportError("report_identity_mismatch", "Analysis state is invalid") from error
        analysis = self._analysis_document(state)
        if state.get("latest_success_generation") is None:
            if state.get("status") in PENDING_ANALYSIS_STATUSES:
                status, should_poll = "analysis_pending", True
            elif state.get("status") == "failed":
                status, should_poll = "analysis_failed", False
            else:
                status, should_poll = "report_not_available", False
            return self._document(
                status,
                False,
                should_poll,
                mr_document,
                {"id": cycle_id},
                analysis,
                None,
            )

        report = self.report_store.get_report(cycle_id)
        self._validate_report_state_identity(cycle_id, state, report)
        try:
            build = self.coverage_store.get_build(str(report["current_build_id"]))
        except CoverageStoreError as error:
            raise CoverageReportError("report_identity_mismatch", "Report Build is invalid") from error
        build_diff = self._validated_build_diff(cycle_id, build)
        report_document = self._report_document(cycle_id, report, build_diff)
        matches = (
            build_diff["base_sha"] == merge_request.base_sha
            and build_diff["start_sha"] == merge_request.start_sha
            and build_diff["head_sha"] == merge_request.head_sha
        )
        if not matches:
            return self._document(
                "stale_diff",
                False,
                state.get("status") in PENDING_ANALYSIS_STATUSES,
                mr_document,
                {"id": cycle_id},
                analysis,
                report_document,
            )
        if state.get("status") in PENDING_ANALYSIS_STATUSES:
            status, should_poll = "analysis_pending", True
        elif state.get("status") == "failed":
            status, should_poll = "analysis_failed", False
        else:
            status, should_poll = "ready", False
        return self._document(
            status,
            True,
            should_poll,
            mr_document,
            {"id": cycle_id},
            analysis,
            report_document,
        )

    def resolve_presentation(self, mr_url, page_version, presentation_id=None, file_id=None):
        """Negotiates schema 2; schema 1 retains its original strict-SHA behavior."""

        normalized = normalize_mr_url(mr_url, self.gitlab_client.project_registry)
        if not isinstance(page_version, str) or re.fullmatch(r"[1-9][0-9]{0,18}", page_version) is None:
            raise CoverageReportError("page_version_unverified", "Page diff version cannot be verified")
        scope = (normalized.project.project, str(normalized.iid), page_version)
        if file_id is not None:
            with self._presentation_lock:
                cached = self._presentations.get(presentation_id)
            if cached is None or cached[0] != scope or file_id not in cached[1]["details"]:
                raise CoverageReportError("presentation_expired", "MR presentation must be refreshed")
            snapshot = cached[1]
            return {"schema_version": 2, "presentation_id": presentation_id, "page_version": page_version,
                    "cycle_id": snapshot["cycle_id"], "mr_iid": scope[1], "generation": snapshot["generation"],
                    **copy.deepcopy(snapshot["details"][file_id])}
        result = self.resolve(mr_url)
        result["schema_version"] = 2
        result["page_version"] = page_version
        result["presentation_id"] = None
        result["projection"] = None
        if result["report"] is None:
            return result
        page_diff = self.gitlab_client.fetch_version_refs(mr_url, page_version)
        if page_diff != result["mr"]["diff"]:
            result.update(status="page_version_unverified", can_annotate=False)
            return result
        cycle_id = result["cycle"]["id"]
        report = self.report_store.get_report(cycle_id)
        reference = result["report"]
        if any(report.get(field) != reference.get(field) for field in ("generation", "current_build_id", "updated_at")):
            raise CoverageReportError("report_identity_mismatch", "Report changed during MR resolution")
        independent = self.report_store.get_mr_evidence_references(cycle_id, report)
        identity = {"version": "mr-lines/4", "scope": scope, "cycle_id": cycle_id, "diff": page_diff,
                    "generation": report["generation"], "build_id": report["current_build_id"],
                    "orchestration": report["orchestration_input_sha256"], "inputs": report["cycle_input_set_sha256"],
                    "independent_evidence": independent}
        key = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()
        def create_snapshot():
            """Computes a request-confined projection outside cache locks."""

            strict = page_diff == reference["build_diff"] and not independent
            if strict:
                snapshot = self.report_store.get_mr_report_snapshot(cycle_id, report)
            else:
                definition = normalized.project
                repository = self.gitlab_client.project_registry.repository(definition.project)
                evidence = self.report_store.get_mr_evidence(cycle_id, report, independent)
                try:
                    snapshot = MergeRequestPresentation(repository, definition, self.coverage_store).project(
                        cycle_id, page_diff, evidence, {"build_id": report["current_build_id"],
                            **self.report_store.get_mr_report_snapshot(cycle_id, report)})
                except LOCAL_ERRORS as error:
                    raise CoverageReportError("mr_data_unavailable", "Current MR Git data is unavailable") from error
            snapshot = {**snapshot, "cycle_id": cycle_id, "generation": report["generation"], "strict_match": strict}
            return snapshot

        presentation_id, snapshot = self._cached_presentation(key, scope, create_snapshot)
        # No identity is inferred from generation alone, including base/start-only changes.
        result["presentation_id"] = presentation_id
        result["projection"] = copy.deepcopy({field: snapshot[field] for field in ("files", "summary", "strict_match")})
        result["can_annotate"] = True
        result["should_poll"] = True
        analysis_status = result["analysis"]["status"]
        result["status"] = "analysis_pending" if analysis_status in PENDING_ANALYSIS_STATUSES else (
            "analysis_failed" if analysis_status == "failed" else "ready")
        return result

    def _cached_presentation(self, input_key, scope, create_snapshot):
        """Single-flights input computation; issued IDs permanently bind actual contents.

        Transiently degraded entries are retried after a bounded interval. An old
        issued ID still reads its original immutable details during recovery.
        """

        with self._presentation_lock:
            current = self._presentation_inputs.get(input_key)
            if current is not None:
                issued_id, retry_at = current
                cached = self._presentations.get(issued_id)
                if cached is not None and (retry_at is None or time.monotonic() < retry_at):
                    return issued_id, cached[1]
            pending = self._projection_pending.get(input_key)
            owner = pending is None
            if owner:
                pending = Future()
                self._projection_pending[input_key] = pending
        if not owner:
            return pending.result()
        try:
            with self._projection_slots:
                snapshot = create_snapshot()
                encoded = json.dumps(snapshot, sort_keys=True, separators=(",", ":"))
                snapshot = json.loads(encoded)
                issued_id = hashlib.sha256((input_key + ":" + encoded).encode()).hexdigest()
            with self._presentation_lock:
                self._presentations.setdefault(issued_id, (scope, snapshot))
                retry_at = time.monotonic() + 15 if snapshot.get("retryable") else None
                self._presentation_inputs[input_key] = (issued_id, retry_at)
                while len(self._presentations) > 8:
                    removed_id, _ = self._presentations.popitem(last=False)
                    self._presentation_inputs = {key: value for key, value in self._presentation_inputs.items()
                                                 if value[0] != removed_id}
            pending.set_result((issued_id, snapshot))
            return issued_id, snapshot
        except BaseException as error:
            pending.set_exception(error)
            raise
        finally:
            with self._presentation_lock:
                self._projection_pending.pop(input_key, None)

    def _validated_build_diff(
        self, cycle_id: str, build: Mapping[str, object]
    ) -> Dict[str, str]:
        """Returns the exact immutable Build comparison snapshot after identity checks."""

        if build.get("cycle_id") != cycle_id or build.get("artifact_state") != "ready":
            raise CoverageReportError("report_identity_mismatch", "Report Build identity changed")
        fields = {
            "base_sha": build.get("merge_base_sha"),
            "start_sha": build.get("target_sha_at_build"),
            "head_sha": build.get("git_sha"),
        }
        if any(
            not isinstance(value, str) or GIT_SHA_PATTERN.fullmatch(value) is None
            for value in fields.values()
        ):
            raise CoverageReportError("report_identity_mismatch", "Report Build diff is invalid")
        return {field: str(value) for field, value in fields.items()}

    def _validate_report_state_identity(
        self,
        cycle_id: str,
        state: Mapping[str, object],
        report: Mapping[str, object],
    ) -> None:
        """Rejects a report whose projected identity differs from persisted latest success."""

        expected = {
            "cycle_id": cycle_id,
            "generation": state.get("latest_success_generation"),
            "current_build_id": state.get("latest_success_current_build_id"),
            "orchestration_input_sha256": state.get(
                "latest_success_orchestration_sha256"
            ),
            "cycle_input_set_sha256": state.get("latest_success_cycle_input_sha256"),
        }
        if any(report.get(field) != value for field, value in expected.items()):
            raise CoverageReportError("report_identity_mismatch", "Report identity changed")

    def _analysis_document(self, state: Mapping[str, object]) -> Dict[str, object]:
        """Projects only the scheduler fields needed by a polling browser client."""

        return {
            "status": state.get("status"),
            "requested_generation": state.get("requested_generation"),
            "completed_generation": state.get("completed_generation"),
        }

    def _report_document(
        self,
        cycle_id: str,
        report: Mapping[str, object],
        build_diff: Dict[str, str],
    ) -> Dict[str, object]:
        """Projects one safe report reference without filesystem or artifact paths."""

        summary = report.get("summary")
        if not isinstance(summary, dict) or any(field not in summary for field in SUMMARY_FIELDS):
            raise CoverageReportError("report_format_unsupported", "Report summary is invalid")
        return {
            "generation": report.get("generation"),
            "updated_at": report.get("updated_at"),
            "current_build_id": report.get("current_build_id"),
            "summary": {field: summary[field] for field in SUMMARY_FIELDS},
            "build_diff": build_diff,
            "summary_api": "/api/v1/cycles/{}/report".format(cycle_id),
        }

    def _mr_document(self, merge_request: GitLabMergeRequest) -> Dict[str, object]:
        """Projects validated GitLab identity and all three current diff refs."""

        definition = self.gitlab_client.project_registry.require(merge_request.project)
        return {
            "host": definition.gitlab.host,
            "project_path": definition.gitlab.project_path,
            "iid": str(merge_request.iid),
            "canonical_url": merge_request.web_url,
            "diff": {
                "base_sha": merge_request.base_sha,
                "start_sha": merge_request.start_sha,
                "head_sha": merge_request.head_sha,
            },
        }

    def _normalized_mr_document(self, normalized: NormalizedMRURL) -> Dict[str, object]:
        """Returns safe URL identity when GitLab has no annotatable diff snapshot."""

        definition = normalized.project
        return {
            "host": definition.gitlab.host,
            "project_path": definition.gitlab.project_path,
            "iid": str(normalized.iid),
            "canonical_url": "https://{}/{}/merge_requests/{}".format(
                definition.gitlab.host, definition.gitlab.project_path, normalized.iid
            ),
            "diff": {"base_sha": None, "start_sha": None, "head_sha": None},
        }

    def _document(
        self,
        status: str,
        can_annotate: bool,
        should_poll: bool,
        mr: Dict[str, object],
        cycle: Optional[Dict[str, object]],
        analysis: Optional[Dict[str, object]],
        report: Optional[Dict[str, object]],
    ) -> Dict[str, object]:
        """Builds the fixed top-level resolver response schema."""

        return {
            "schema_version": 1,
            "status": status,
            "can_annotate": can_annotate,
            "should_poll": should_poll,
            "mr": mr,
            "cycle": cycle,
            "analysis": analysis,
            "report": report,
        }
