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
- `scheduler.py`: single worker; `command_runner.py`: argv-only subprocess evidence.

## Dependency direction and data

The orchestrator calls the three analyzers; analyzers share mapping/parser utilities and fixed integration boundaries. They read SQLite snapshots and immutable files and publish only under root runtime `analysis/`. No analyzer reads mutable working-tree source.

Tests are under `tests/analysis/`, with scheduler integration under `tests/server/test_automatic_analysis.py`.
