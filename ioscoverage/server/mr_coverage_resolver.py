"""Read-only GitLab MR, Cycle, Build, and report identity resolver."""

import re
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
