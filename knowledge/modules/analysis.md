# Analysis module

## Responsibility

Validate immutable registered inputs, run fixed Xcode 26.3 LLVM commands per Build, map the current Build to its fixed Git diff, conservatively accumulate unchanged historical regions, publish content-addressed evidence, and orchestrate all valid inputs for a Cycle.

## Non-responsibility

No HTTP parsing, Run upload, schema migration, Token handling, Git checkout/worktree mutation, GitLab API, browser rendering, or cross-Build profile merge.

## Entry files and key types

- `single_build/analyzer.py`: `SingleBuildAnalyzer`, `AnalysisConfiguration`.
- `diff/analyzer.py`: `SingleBuildDiffAnalyzer`; `diff/git_diff_parser.py`: NUL status and zero-context patch parsing.
- `cumulative/analyzer.py`: `CycleCoverageAnalyzer` and historical hit selection.
- `mapping/coverage_export.py`: llvm-cov region normalization.
- `mapping/source_region.py`: exact Git line/range inheritance evidence for formal reports.
- `mr_presentation.py`, `mapping/mr_lines.py`: read-only current-MR display mapping, separate from formal region inheritance; see `workflows/coverage_report.md`.
- `orchestrator.py`: `CycleAnalysisOrchestrator` snapshot selection and stage coordination; missing/untested-current selection also refreshes independent Build evidence without publishing a formal Cycle success.
- `scheduler.py`: single generation worker and shutdown coordination.
- `process_orchestrator.py` / `process_worker.py`: production-only, terminable process-group boundary around the unchanged Cycle orchestrator.
- `command_runner.py`: argv-only subprocess evidence.
- `storage/retained_evidence.py`: verified same-Build merged-profile baseline used after raw profile compaction.

## Dependency direction and data

The orchestrator calls the three analyzers; analyzers share mapping/parser utilities and fixed integration boundaries. They read SQLite snapshots and immutable files and publish only under root runtime `analysis/`. No analyzer reads mutable working-tree source. The HTTP service executes each orchestration attempt in one child process group; its LLVM and Git descendants inherit that group. The process adapter keeps ownership until every member has exited, including when the Python leader fails first, and applies bounded TERM then KILL cleanup. If termination cannot be confirmed, the scheduler and durable generation remain running, cleanup stays blocked, and no replacement analysis may overwrite ownership of the unresolved group. The failure transition occurs only after absence is proven, so a post-commit persistence exception cannot open the cleanup barrier early. The worker and every controlled subprocess also inherit the parent's `server.lock` lease; an abrupt parent exit therefore leaves runtime ownership attached to the surviving analysis execution. Diagnostic CLIs continue to call the same orchestrator directly while holding the runtime process lock.

A permission-denied signal-0 probe is not proof that a process group has exited. The adapter conservatively treats it as active and continues the existing bounded exit wait; only a process-not-found result proves absence. Persistent denial retains execution ownership and cleanup blocking under the existing termination-failure path.

After cleanup, `SingleBuildAnalyzer` treats incorporated Runs as one Cycle/Build-scoped validated baseline and merges it only with new profiles from that same Build. Diff and cumulative input validation accept a missing non-coverage artifact only when completion-protected retained evidence authorizes that exact Build role; the selected coverage binary remains mandatory. The input-set identity still contains every Run identity, and the cumulative analyzer continues to consume one independent export per Build. Input orchestration resolves that retained baseline once per Build, avoiding repeated hashes of large immutable exports for each missing raw profile.

LLVM compiler paths are mapped within the Server analysis layer. New Debug packages use normalized repository-relative coverage names, checked by exact membership in the Build's fixed Git tree and validated against fixed source blobs. They never resolve against the server working directory or select a compiler root; dot/traversal components, empty components, and backslash aliases are rejected. Canonical absolute paths from legacy packages retain filesystem resolution, including local symlink aliases. Foreign absolute compiler roots are authorized only through the project's immutable `compiler_repository_names` directory-name allowlist, defaulting to the registered repository basename. With the Build's fixed `git_sha` tree and source loader, the analyzer finds one unique allowlisted directory boundary whose remaining full relative path exists in that tree. It then reuses that directory-boundary prefix for the remaining compiler paths instead of comparing each filename against all Git paths. Exceptional paths cannot introduce a second valid root; a one-time index of nested allowlisted splits in Git paths preserves ambiguity rejection even after prefix reuse. Multiple valid splits or roots fail with `coverage_path_mapping_ambiguous`. Unregistered roots are not inferred. Generated/external files without a fixed Git path remain unmapped. Each normalization owns its prefix and indexes locally; no cross-Build or shared mutable cache is introduced.

Cross-Build function comparison preserves the complete symbol and normalizes only a source-file qualifier tied to the mapped full relative path (or the existing Clang basename qualifier). Swift can retain an absolute source qualifier even after coverage filename prefix mapping. Different symbols or qualifiers for a different source path do not inherit hits. Region ranges, fixed source bytes, and Git line mapping still must match.

Mapped regions and segments retain their fixed Git blob coordinate checks. If measurable code regions or file segments exist but no repository code region maps, analysis fails with `coverage_path_mapping_failed` rather than publishing an all-`not_measurable` report. `coverage_path_source_mismatch` remains a safe failure code. Normalized evidence uses repository-relative filename fields and records unmapped files only as a count. Internal region `function_name` values preserve LLVM's raw function identities and may still contain absolute source qualifiers; function comparison handles those qualifiers without rewriting the stored identity. Public report region projections exclude function names. Both diff and cumulative analysis receive the allowlist through `AnalysisConfiguration` from the same startup-fixed ProjectRegistry snapshot; production workers receive it in the private registry transport. The policy participates in orchestration, diff, and cumulative identities, and mapping changes bump all three versions so retry cannot reuse an old complete report before revalidation. Build/Run identity and persistence are unchanged.

The foreign-root policy relies on a trusted packaging workflow: the credential holder uses `coveragectl` on the intended project, keeps its worktree clean and Git SHA frozen from prepare through register, and archives that checkout. The Server verifies the declared commit in the registered repository and the uploaded artifact identities, but cannot independently prove from the LLVM export that the Archive was compiled from the exact Git blob bytes. A deliberately or accidentally mismatched Archive with matching repository-relative source paths can pass geometric checks. This is the explicit trust limit of existing Build metadata; it is not a source-content attestation.

Tests are under `tests/analysis/`, with scheduler integration under `tests/server/test_automatic_analysis.py`.
