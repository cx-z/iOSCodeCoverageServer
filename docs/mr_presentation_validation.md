# Current MR presentation validation

The schema-2 MR projection is independent of formal Cycle coverage. Run from the server root:

```sh
python3 -m unittest tests.analysis.test_mr_lines tests.analysis.test_mr_presentation tests.analysis.test_independent_mr_evidence tests.analysis.test_orchestrator tests.lifecycle.test_runtime_cleanup tests.lifecycle.test_runtime_cleanup_manager tests.server.test_mr_presentation_protocol tests.server.test_gitlab_coverage_resolver tests.web.test_report_ui tests.integrations.test_gitlab_mr tests.analysis.test_cycle_coverage tests.analysis.test_single_build_diff tests.test_architecture
python3 scripts/check_public_repository.py
```

Run from the extension root:

```sh
node --test tests/*.test.js
```

## Product coverage matrix

| Product cases | Automated evidence |
| --- | --- |
| P01–P05 | Real temporary Git snapshots: partial edits inside one multi-line region, consecutive commits, insertion/deletion and restore; large repeated files with unique complete embeddings and bounded edit bands |
| P06–P07 | Copy/new-file rejection, repeated-line ambiguity, large unique-line reversal through the final budget fallback compared with the full-alignment oracle; one-to-one rename with a partial edit plus unrelated added files; true competing rename sources remain unknown |
| P08–P11 | Actual scheduler → immutable independent Build publication → storage → fixed-Git projection with missing/untested current Build, using a controlled LLVM output boundary; untested package supplies no evidence, old package cannot light changed code, full-line hit requirement, complete published mixed-source evidence retained |
| P12 | Changed fixed diff range; independent head/base/start cache invalidation with generation unchanged |
| P13 | Local source/export failure; other valid hits survive; DOM ambiguity affects only its own file |
| P14 | Report/input identity and page version in cache key; single-flight publication, content-bound issued IDs, retry/recovery without rebinding old IDs; late resolver authorization, late file response, same-generation snapshot replacement and route/version render cancellation |
| P15 | No successful report, stale page-selected version and malformed/unsupported response fail closed |
| P16–P17 | Current projection file counts, filter counts, no mismatched-MR percentage, uniform red label |
| P18 | Git path/hunk plus alignment ambiguity veto; actual row three-ref/path/line validation |
| P19 | Viewport deferral, four-request limit, progressive async file insertion, batch yielding, mutation locality, repeated-update skipping and cancellation |
| P20 | Original strict report projection, schema-1 stale gate, formal report storage/HTTP and cumulative analysis regression |

Raw Run upload alone is not coverage evidence: the existing independent analysis must produce a valid successful result before new hits become available. The read-only MR resolver never triggers analysis. Old-package lifecycle tests run actual scheduler/orchestration/publication/storage with controlled LLVM exports. They verify new A Runs produce applicable evidence while current Build selection still fails, changed code stays red, formal success stays unchanged, and later formal incorporation restores the strict path. Actual production IPA upload and LLVM execution remain separate integration acceptance; these tests do not claim to replay production packages.

When both full and banded alignment budgets are exceeded, the mapper authorizes no unproven inherited pairs. Common rows are conservatively unknown; definite novel rows remain uncovered. The large-reversal test uses real Git snapshots and compares the production fallback with a temporarily enlarged exact-proof budget on the same objects. Deterministic shuffled-row differential cases also force budget exhaustion and reject any positive absent from the exact oracle.

## Real GitLab acceptance

Use the installed development extension and the deployed service, with a registered MR having a successful historical Build report and newer current code. Read live identities and analysis state; do not reuse a prior session's numbers. Do not store private source, screenshots, credentials, runtime IDs or one-off measurements in knowledge documents.

1. Reload the ordinary latest Changes page. During GitLab's own asynchronous diff insertion, verify code becomes readable independently of coverage. After resolution verify category counts cover the complete current new side, with the historical-evidence label and no current-MR percentage for a mismatch.
2. Navigate/scroll to another file. Verify markers appear near the viewport and the page stays usable. Expand one collapsed file: rows may appear before coverage, and file-detail latency must not block the code. A slow file must not block other files.
3. Enable/disable the uncovered filter and confirm it uses server current-MR red counts, including files whose old report had zero uncovered lines.
4. Switch Inline/Side-by-side. Old detached rows must lose markers; newly loaded rows must pass their own structured `data-position` identity before annotation.
5. Open a historical diff or retain an old page version while the MR advances. Confirm no latest-snapshot results are applied there. Reload latest and refresh the report; delayed old results must not restore earlier state.
6. Check the Cycle report independently: its source snapshot, original counts and formal percentage must remain unchanged.

For very large MRs where GitLab itself stalls on **Expand all** even without coverage, full-expansion usability is a user-owned manual check. Passing viewport and single-file real-page checks does not claim that full-expansion case passed. Automated DOM scheduler tests supplement, and do not replace, the real-page checks above.
