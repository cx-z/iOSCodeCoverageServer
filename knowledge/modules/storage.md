# Storage module

## Responsibility

Persist stable Cycle/Build/Run metadata, platform users, hashed Sessions, and user-Cycle submission relations in SQLite; publish verified files atomically, maintain analysis-generation state, project immutable reports, and manage owner-only local tokens.

## Non-responsibility

No HTTP status selection, browser rendering, LLVM/Git analysis, or GitLab network calls.

## Entry files and key types

- `coverage_store.py`: stable `CoverageStore` facade and constructor.
- `cycles.py`, `builds.py`, `runs.py`: resource persistence methods. `create_or_get_cycle_for_user` atomically creates/reuses a Cycle and records the submitter; developer list/detail reads enforce that relation.
- `accounts.py`: AccountStore user/password/session lifecycle. Passwords use salted scrypt; Session rows store only SHA-256 and are revoked atomically on disable/reset.
- `database.py`: connection/transaction context; `schema.py`: schema v4, existing triggers, account/session/submission tables, and transactional v3→v4 migration.
- `artifact_files.py`: staging, hashing, fsync, atomic paths; `validation.py` and `common.py`: stable field/error rules.
- `analysis_state.py`: `AnalysisStateStore` generation state machine.
- `reports.py`: `CoverageReportStore` safe latest-success projection.
- `retained_evidence.py`: validates and durably publishes Cycle/Build-scoped compaction evidence tied to immutable single-Build results; existing evidence is re-fsynced before reuse and obsolete proven records are cleanup candidates.
- `tokens.py`: `TokenStore` atomic persistent operator, packaging, run_upload, and GitLab credentials.

## Dependency and data

CoverageStore receives immutable `ProjectRegistry` and `XcodeArtifactValidator` collaborators. Build validation resolves Cycle.project to the correct repository and Bundle ID allowlist. It owns one process-local mutation lock; online cleanup takes that same guard only after the analysis worker is paused and idle. Connections are short-lived and never shared across request/worker threads. Runtime files remain below one RuntimePaths root.

Relevant tests live under `tests/storage/`, `tests/server/test_api_contracts.py`, `tests/server/test_automatic_analysis.py`, and analyzer integrity tests.

Runtime cleanup itself is in `ioscoverage.lifecycle.cleanup`; storage exposes retained evidence so idempotent Build/Run replay can distinguish intentional compaction from missing or corrupt files. Cleanup rebinds every incorporated Run to the current read-only SQLite snapshot and requires exact file size/SHA-256 before deleting raw profiles or Build artifacts. Orchestration resolves and validates at most one compatible baseline per selected Build, then compares all missing Runs against that in-memory identity map.
