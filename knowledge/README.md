# iOS Coverage Platform server knowledge entry

This is the mandatory first read for engineering work. It routes an agent to a small, relevant slice of the codebase; protocol details remain in `docs/api.md`, and current code remains the final source of truth.

## Product purpose

The server receives immutable iOS Build artifacts and user-triggered `.profraw` Runs for registered projects. It analyzes each Build independently, intersects the current Build with its fixed Git diff, and conservatively inherits unchanged historical region hits.

## Core concepts

- **ProjectRegistry**: immutable mapping from Cycle.project to repository, GitLab project, and Bundle ID allowlist.
- **Cycle**: one registered-project MR testing lifetime. `base_sha` is initial audit data; `head_sha` selects the current requirement commit.
- **Build**: one immutable package identity: Git snapshot, coverage binary, dSYM, UUIDs, sizes, and hashes.
- **Run**: one immutable `.profraw` snapshot tied to its original Cycle and Build.
- **current Build**: the latest ready Build whose `git_sha` equals the Cycle `head_sha`.
- **analysis generation**: durable scheduling state. New analyzable input requests a newer generation; the last successful report remains available on failure.
- **retained Build evidence**: completion-protected binding from incorporated Runs and validated artifacts to one same-Build merged profile and LLVM export, allowing safe raw-input compaction.

## Data flow

```text
MR URL -> GitLab read-only MR lookup -> Cycle
packaging Mac -> Build manifest + Mach-O/dSYM -> storage
iPhone user upload -> Run metadata + profraw -> storage -> scheduler
scheduler -> Cycle orchestrator
  -> per-Build LLVM export
  -> current fixed Git diff
  -> cross-Build source-region accumulation
  -> immutable analysis result
browser -> protected report API -> latest successful result
```

## Top-level modules

```text
root compatibility commands
  -> ioscoverage.cli / ioscoverage.server.bootstrap
RuntimePaths + ProjectRegistry -> mutable paths and trusted project contexts
ioscoverage.server.http -> storage facades, analysis scheduler, integrations, web pages
ioscoverage.analysis -> storage snapshots + fixed Git/Xcode integrations
ioscoverage.web -> templates/static assets only; no database or token access
<runtime_root>/{config,state,instance,data,analysis,logs} -> never source packages
```

See [architecture](architecture.md) for allowed dependencies and [invariants](invariants.md) before changing shared contracts.

## Daily entry points

- Start/diagnose service: configure `config/server.json`, then run `./start_coverage_server.sh`; use `python3 server.py --help` for options
- Cycle list and report: `/cycles/`
- Diagnostic analysis only: the four root `analyze_*.py` compatibility commands
- Runtime cleanup: `python3 cleanup_runtime_data.py` (dry-run), then stop the server before `--apply`
- User workflow: `docs/daily_workflow.md`
- HTTP contract: `docs/api.md`

## Read-next routing

| Task | Read next |
| --- | --- |
| Service startup, HTTP route, auth, multipart, error or logging | `modules/server.md`, then the relevant workflow |
| Browser list/report/probe page | `modules/web.md`, `workflows/coverage_report.md` |
| Cycle/Build/Run persistence, SQLite or file integrity | `modules/storage.md`, relevant workflow |
| LLVM, diff, cumulative mapping or orchestration | `modules/analysis.md`, `workflows/automatic_analysis.md` |
| Git, GitLab or Xcode validation | `modules/integrations.md` and the calling workflow |
| CLI or startup script | `modules/tools.md` |
| Runtime growth, retained evidence, or cleanup | `modules/storage.md`, `modules/analysis.md`, `workflows/runtime_cleanup.md` |
| Public repository safety or release-candidate scan | `modules/tools.md`, `invariants.md` |
| Unsure where a symptom belongs | `code_map.md` |
