"""Read-only integration routes for browser clients."""

from http import HTTPStatus
import re

from ioscoverage.integrations.gitlab_mr import GitLabMRClientError
from ioscoverage.storage.coverage_store import CoverageStoreError
from ioscoverage.storage.reports import CoverageReportError


class IntegrationRoutesMixin:
    """Converts the fixed GitLab MR resolver request into one safe JSON response."""

    def _handle_gitlab_merge_request_coverage_resolve(self) -> None:
        """Resolves one MR URL without creating data or scheduling analysis."""

        try:
            payload = self._read_json_body()
            schema_version = payload.get("schema_version")
            if type(schema_version) is not int or schema_version not in (1, 2):
                raise CoverageStoreError("invalid_schema_version", "schema_version must be 1 or 2")
            expected = {"schema_version", "mr_url"}
            if schema_version == 2:
                expected.add("page_version")
                if not isinstance(payload.get("page_version"), str) or re.fullmatch(r"[1-9][0-9]{0,18}", payload["page_version"]) is None:
                    raise CoverageStoreError("invalid_page_version", "page_version must be a numeric diff ID")
                if "file_id" in payload or "presentation_id" in payload:
                    expected.update(("file_id", "presentation_id"))
                    if any(not isinstance(payload.get(field), str) or re.fullmatch(r"[0-9a-f]{64}", payload[field]) is None
                           for field in ("file_id", "presentation_id")):
                        raise CoverageStoreError("invalid_fields", "MR file identity is invalid")
            if set(payload) != expected:
                raise CoverageStoreError("invalid_fields", "request fields are invalid")
            resolver = self.config.merge_request_coverage_resolver
            if resolver is None:
                self._send_json(
                    HTTPStatus.SERVICE_UNAVAILABLE,
                    {
                        "error": "merge_request_coverage_resolver_unavailable",
                        "message": "merge request coverage resolver is unavailable",
                    },
                )
                return
            if schema_version == 1:
                document = resolver.resolve(payload.get("mr_url"))
            else:
                document = resolver.resolve_presentation(payload.get("mr_url"), payload.get("page_version"),
                    payload.get("presentation_id"), payload.get("file_id"))
        except GitLabMRClientError as error:
            self._send_gitlab_error(error)
            return
        except CoverageReportError as error:
            self._send_report_error(error)
            return
        except (CoverageStoreError, ValueError) as error:
            self._send_domain_error(error)
            return
        cycle = document.get("cycle")
        cycle_id = cycle.get("id") if isinstance(cycle, dict) else document.get("cycle_id")
        mr = document.get("mr")
        mr_iid = mr.get("iid") if isinstance(mr, dict) else document.get("mr_iid")
        self._log_event(
            "merge_request_coverage_resolved",
            cycle_id=cycle_id,
            mr_iid=mr_iid,
            resolution_status=document.get("status", "file"),
            can_annotate=document.get("can_annotate", True),
        )
        self._send_json(HTTPStatus.OK, document)
