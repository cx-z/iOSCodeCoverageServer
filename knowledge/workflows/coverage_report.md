# Coverage report workflow

1. **Entry:** `/cycles/`, `/cycles/<cycle_id>`, and protected report JSON endpoints.
2. **Call chain:** page route -> web shell; browser -> analysis/report API -> `CoverageReportStore`.
3. **Persistence:** none. The store derives fixed result directories from latest-success identities and validates completion/file SHA before projection.
4. **Background trigger:** none; two-second page refresh is read-only.
5. **Success:** summary, safe file IDs, changed-line states, source text already stored in analysis output, and current/historical evidence labels.
6. **Failure:** missing/tampered results are rejected; queued/running/failed states may continue showing the prior latest success.

The Web layer never receives arbitrary filesystem paths and browser text uses safe DOM projection. Tests: `tests/web/test_report_ui.py`. Guide: `docs/coverage_report_ui.md`.
