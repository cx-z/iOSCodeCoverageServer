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
- `mapping/source_region.py`: exact Git line/range inheritance evidence.
- `orchestrator.py`: `CycleAnalysisOrchestrator` snapshot selection and stage coordination.
- `scheduler.py`: single generation worker and shutdown coordination.
- `process_orchestrator.py` / `process_worker.py`: production-only, terminable process-group boundary around the unchanged Cycle orchestrator.
- `command_runner.py`: argv-only subprocess evidence.
- `storage/retained_evidence.py`: verified same-Build merged-profile baseline used after raw profile compaction.

## Dependency direction and data

The orchestrator calls the three analyzers; analyzers share mapping/parser utilities and fixed integration boundaries. They read SQLite snapshots and immutable files and publish only under root runtime `analysis/`. No analyzer reads mutable working-tree source. The HTTP service executes each orchestration attempt in one child process group; its LLVM and Git descendants inherit that group. The process adapter keeps ownership until every member has exited, including when the Python leader fails first, and applies bounded TERM then KILL cleanup. If termination cannot be confirmed, the scheduler and durable generation remain running, cleanup stays blocked, and no replacement analysis may overwrite ownership of the unresolved group. The failure transition occurs only after absence is proven, so a post-commit persistence exception cannot open the cleanup barrier early. The worker and every controlled subprocess also inherit the parent's `server.lock` lease; an abrupt parent exit therefore leaves runtime ownership attached to the surviving analysis execution. Diagnostic CLIs continue to call the same orchestrator directly while holding the runtime process lock.

After cleanup, `SingleBuildAnalyzer` treats incorporated Runs as one Cycle/Build-scoped validated baseline and merges it only with new profiles from that same Build. Diff and cumulative input validation accept a missing non-coverage artifact only when completion-protected retained evidence authorizes that exact Build role; the selected coverage binary remains mandatory. The input-set identity still contains every Run identity, and the cumulative analyzer continues to consume one independent export per Build. Input orchestration resolves that retained baseline once per Build, avoiding repeated hashes of large immutable exports for each missing raw profile.

LLVM compiler paths are mapped within the Server analysis layer. Existing canonical absolute paths under the registered repository still map directly. For a different machine, the analyzer enumerates the Build's fixed `git_sha` tree, requires a unique full repository-relative suffix beneath a compiler root whose final component matches the registered repository root, and checks mapped regions and segments against the corresponding fixed Git blob. One export cannot combine distinct accepted foreign roots. External, malformed, and ambiguous paths are not inferred from a basename. If measurable code regions or file segments exist but no repository code region maps, analysis fails with `coverage_path_mapping_failed` instead of publishing an all-`not_measurable` result. `coverage_path_mapping_ambiguous` and `coverage_path_source_mismatch` are also safe failure codes. Compiler paths are omitted from normalized coverage evidence; only an unmapped count is retained. LLVM export has no source-content digest, so coordinate checks cannot cryptographically prove compiler source bytes equal the Git blob; unresolved cases remain conservative. Mapping changes bump orchestration, diff, and cumulative identities so an unchanged-input retry does not reuse an old complete Cycle result before reaching the repaired analyzers.

The foreign-root policy relies on a trusted packaging workflow: the credential holder uses `coveragectl` on the intended project, keeps its worktree clean and Git SHA frozen from prepare through register, and archives that checkout. The Server verifies the declared commit in the registered repository and the uploaded artifact identities, but cannot independently prove from the LLVM export that the Archive was compiled from the exact Git blob bytes. A deliberately or accidentally mismatched Archive with a same-named source tree can pass geometric checks. This is the explicit trust limit of existing Build metadata; it is not a source-content attestation.

Tests are under `tests/analysis/`, with scheduler integration under `tests/server/test_automatic_analysis.py`.
