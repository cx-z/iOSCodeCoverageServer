# Coverage report workflow

1. **Entry:** `/login/` is the account entry, `/cycles/` is the Session-gated workbench, and `/cycles/<cycle_id>` plus report JSON endpoints remain the report entry. When `anonymous_read` is enabled, the resolver, report, and report-file endpoints used by the browser extension are served without a credential; otherwise they require any valid platform principal. The browser UI uses HttpOnly Session and never stores machine Tokens.
2. **Call chain:** page route -> web shell; browser -> analysis/report API -> `CoverageReportStore`.
3. **Persistence:** none. The store derives fixed result directories from latest-success identities and validates completion/file SHA before projection.
4. **Background trigger:** none; two-second page refresh is read-only.
5. **Success:** summary, safe file IDs, changed-line states, source text already stored in analysis output, and current/historical evidence labels.
6. **Failure:** missing/tampered results are rejected; queued/running/failed states may continue showing the prior latest success.

An export containing compiler code regions but no project mapping now fails analysis before a new report is published. Genuine files without compiler regions retain `not_measurable` semantics. A prior latest success remains readable, subject to the existing fixed GitLab diff-ref check.

For a GitLab MR page, the read-only resolver route first performs the registered-project MR GET, looks up the existing Cycle by `project + mr_iid`, validates latest-success completion, reads its current Build, and compares all three immutable Build refs with GitLab base/start/head refs. A matching prior report may remain annotatable while analysis is queued/running/failed; a mismatched diff is always `stale_diff`. This path is read-only and never creates a Cycle or requests analysis.

The Web layer never receives arbitrary filesystem paths and browser text uses safe DOM projection. Tests: `tests/web/test_report_ui.py`, `tests/server/test_gitlab_coverage_resolver.py`. Guide: `docs/coverage_report_ui.md`.
