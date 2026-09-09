# Storage module

## Responsibility

Persist stable Cycle/Build/Run metadata in SQLite, publish verified files atomically, maintain analysis-generation state, project immutable reports, and manage owner-only local tokens.

## Non-responsibility

No HTTP status selection, browser rendering, LLVM/Git analysis, or GitLab network calls.

## Entry files and key types

- `coverage_store.py`: stable `CoverageStore` facade and constructor.
- `cycles.py`, `builds.py`, `runs.py`: resource persistence methods. Cycle reads include the stable `project + mr_iid` lookup used by the MR resolver; it never creates a missing Cycle.
- `database.py`: connection/transaction context; `schema.py`: unchanged schema and triggers.
- `artifact_files.py`: staging, hashing, fsync, atomic paths; `validation.py` and `common.py`: stable field/error rules.
- `analysis_state.py`: `AnalysisStateStore` generation state machine.
- `reports.py`: `CoverageReportStore` safe latest-success projection.
- `tokens.py`: `TokenStore` atomic persistent credentials.

## Dependency and data

CoverageStore receives immutable `ProjectRegistry` and `XcodeArtifactValidator` collaborators. Build validation resolves Cycle.project to the correct repository and Bundle ID allowlist. It owns one process-local mutation lock; connections are short-lived and never shared across request/worker threads. Runtime files remain below one RuntimePaths root.

Relevant tests live under `tests/storage/`, `tests/server/test_api_contracts.py`, `tests/server/test_automatic_analysis.py`, and analyzer integrity tests.
