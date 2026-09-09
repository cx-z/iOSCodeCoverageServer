"""Read-only integration routes for browser clients."""

from http import HTTPStatus

from ioscoverage.integrations.gitlab_mr import GitLabMRClientError
from ioscoverage.storage.coverage_store import CoverageStoreError
from ioscoverage.storage.reports import CoverageReportError


class IntegrationRoutesMixin:
    """Converts the fixed GitLab MR resolver request into one safe JSON response."""

    def _handle_gitlab_merge_request_coverage_resolve(self) -> None:
        """Resolves one MR URL without creating data or scheduling analysis."""

        try:
            payload = self._read_json_body()
            if set(payload) != {"schema_version", "mr_url"}:
                raise CoverageStoreError(
                    "invalid_fields",
                    "request must contain only schema_version and mr_url",
                )
            schema_version = payload.get("schema_version")
            if schema_version != 1 or isinstance(schema_version, bool):
                raise CoverageStoreError(
                    "invalid_schema_version", "schema_version must be 1"
                )
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
            document = resolver.resolve(payload.get("mr_url"))
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
        cycle_id = cycle.get("id") if isinstance(cycle, dict) else None
        mr = document.get("mr")
        mr_iid = mr.get("iid") if isinstance(mr, dict) else None
        self._log_event(
            "merge_request_coverage_resolved",
            cycle_id=cycle_id,
            mr_iid=mr_iid,
            resolution_status=document["status"],
            can_annotate=document["can_annotate"],
        )
        self._send_json(HTTPStatus.OK, document)
