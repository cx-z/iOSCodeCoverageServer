# Runtime data lifecycle

## Purpose and scope

The server keeps the latest successful report readable while compacting inputs that have already been incorporated into verified analysis evidence. Production cleanup runs inside the server at startup, every 24 hours from that process start, or after an admin/operator request. It does not delete SQLite resource rows or relax coverage inheritance.

Restarting the server resets the in-memory 24-hour timer; no cleanup deadline or cleanup result is persisted in SQLite. Run `python3 cleanup_runtime_data.py` for an independent dry-run. Stop the server and add `--apply` only when using the offline fallback.

## Read dependencies

The report page and browser integration read `cycle_analysis_states.latest_success_*`, the matching `analysis/cycle-latest` result, and its referenced `analysis/cycle-coverage` result. They validate completion manifests and file size/SHA-256 before reading summary and cumulative changed-line JSON. They do not read Mach-O files, dSYM archives, raw profiles, merged profiles, or old analysis generations.

Without compaction evidence, a later Run for the same Build requires every previously selected `.profraw`, the exact coverage binary, dSYM, and single-Build analysis pipeline. Cross-Build accumulation consumes each Build's independently generated `llvm-cov-export.json` and exact Git objects. It never merges profiles from different Builds.

## Durable retained evidence

For every Build referenced by a Cycle's latest successful orchestration, cleanup publishes a completion-protected record below:

```text
analysis/retained-build/<cycle_id>/<build_id>/<input_set_sha256>/
  manifest.json
  complete.json
```

The record binds the Cycle, Build, Build manifest, actual validated UUID set, incorporated Run identities, removed artifact roles, and exact size/SHA-256 references to the retained single-Build result. Its directory layout and every embedded identity must agree. The retained single-Build result contains `merged.profdata` and `llvm-cov-export.json`.

When a later Run arrives for that Build, the analyzer validates the retained baseline, validates each new raw profile independently, and merges the old `merged.profdata` with only the new same-Build profiles. This preserves earlier hits without retaining incorporated raw profiles. A new Build still uses each historical Build's retained export through the existing strict Git, source-byte, function, and region mapping rules.

The exact `coverage_binary` remains stored because a later same-Build Run still needs it for `llvm-cov export`. dSYM and any non-coverage Mach-O can be removed after the original artifact validation and complete single-Build result are bound into retained evidence.

Downstream diff and cumulative validation continue to verify the immutable Build manifest, SQLite descriptors, UUID metadata, completed LLVM result, and coverage binary. A missing dSYM or other non-coverage Mach-O is accepted only when valid retained evidence for the same Cycle, Build, manifest, and artifact role proves that cleanup intentionally removed it.

## Retention set

The cleanup planner protects:

- SQLite, ProjectRegistry, credentials, logs, and deployment configuration;
- every Cycle's latest verified `cycle-latest` and `cycle-coverage` result;
- the latest orchestration's current diff result;
- every referenced single-Build result and its retained evidence;
- each referenced Build's exact coverage binary;
- Run metadata and upload SHA records;
- raw inputs not incorporated into a verified retained baseline;
- incomplete, corrupt, ambiguous, or unreferenced data that cannot be proven redundant;
- all data for Cycles without a verifiable latest success.

Eligible deletion includes incorporated `.profraw` files, dSYM/non-coverage artifacts, older completion-valid single-Build, diff, cumulative, and orchestration generations for a proven Cycle, and retained-evidence records superseded by the protected Build baseline. Result directories are eligible only when their result kind, fixed path layout, canonical identities, completion manifest, summary, and referenced output bytes agree. The first release deliberately leaves logs, failed/staging directories, and unproven data untouched.

## Safety and execution

The service holds `<runtime_root>/state/server.lock` for its lifetime. Each offline diagnostic analyzer holds the same lock for its complete run. A production analysis worker and its controlled Git/LLVM/Xcode subprocesses inherit the server's lease on that lock, so unexpected parent termination cannot expose the runtime to a new server or offline cleanup while analysis remains alive. Online cleanup is allowed only through the owner of that exact lock. It pauses the single analysis scheduler, waits for the current immutable analysis to finish, and then takes the same process-local mutation guard used by Cycle, Build, and Run persistence. New analysis generations remain queued and uploads may wait briefly for the mutation guard; the HTTP server remains listening and read-only health/report requests continue. If a report request selected an older latest success immediately before a new success was committed, it reloads the latest immutable identity and retries once when cleanup removes the old generation. A `running` analysis state always blocks deletion.

At service startup, an interrupted `running` generation is first recovered to `queued`. Cleanup then runs synchronously after the listen socket is bound but before analysis workers and HTTP serving begin. A cleanup failure is recorded in bounded logs/state and does not prevent the coverage server from starting. The periodic worker begins only afterward and waits a full 24 hours. A manual request and a periodic request are coalesced while cleanup is already active.

Offline `--apply` still obtains the same nonblocking process lock before planning and therefore refuses to run while the server is online. Dry-run remains read-only and may run while the service is online.

All candidates are generated from SQLite and result-kind-specific completion-protected references and normalized below the configured runtime root. Every incorporated Run must still exist in SQLite with the same Cycle, Build, state, analyzability, payload size/SHA-256, and metadata SHA-256. Every file candidate must match the successful result and current manifest by size/SHA-256 when planned and immediately before and after retained-evidence publication. Each directory candidate records the exact relative directory/file set and every file identity. A missing file is accepted only when an already validated retained baseline proves that exact prior compaction.

Symbolic links in managed data or analysis trees cause apply to fail. Formal deletion opens the runtime root once, traverses every ancestor with `O_NOFOLLOW`, and atomically renames each candidate through verified directory descriptors into a mode-`0700`, invocation-private quarantine below `analysis`. The isolated file or complete directory tree is then revalidated against its planned identity before recursive removal. A name replaced before isolation is retained in quarantine on mismatch; a name created after isolation remains at the original location. This closes both ancestor-symlink redirection and the final-check-to-unlink replacement window. A nonempty quarantine is deliberately preserved after an abort rather than deleting unproven bytes.

Cleanup never accesses a ProjectRegistry repository as a deletion target. Retained evidence is atomically renamed and fsynced together with every directory-entry ancestor through the analysis root before raw inputs can be removed; a retry re-establishes the same durability proof even when the evidence directory already exists. Superseded evidence is isolated before its obsolete single-Build result, so an interrupted recursive removal cannot leave a damaged record in the discoverable retained-evidence namespace. Filesystem and evidence-publication failures return bounded error codes rather than local paths or tracebacks.

```bash
# Read-only plan using runtime_root from config/server.json
python3 cleanup_runtime_data.py

# Offline fallback: stop the server, review the plan, then apply it explicitly
python3 cleanup_runtime_data.py --apply
```

Both modes emit one JSON object with scanned Cycle/Build/Run/generation counts, planned and actual item/byte totals by category, skipped reasons, total planned/released bytes, and the `dry_run` flag. Repeating apply is idempotent.

Online status and manual triggering require an admin Session or operator compatibility Token; packaging and restricted iOS Run Tokens are rejected:

```text
GET  /api/v1/maintenance/cleanup
POST /api/v1/maintenance/cleanup
```

POST accepts no body or transfer coding and returns HTTP 202 immediately. Status is process-local and contains only aggregate counts, byte totals, timestamps, the active/last trigger (`startup`, `periodic`, or `manual`), an optional pending manual trigger, at most 20 skipped-item samples, and a bounded error. It never includes credentials or managed file paths. Shutdown cancels an online cleanup that is still waiting for analysis or the mutation guard. Once the destructive guard is acquired, shutdown lets quarantine validation and deletion complete instead of interrupting it. Production Cycle analysis runs in a dedicated process group; SIGTERM enters coordinated shutdown, which terminates that group and joins the scheduler within a bounded grace period. `server.lock` is released only after termination is confirmed. SIGKILL or a crash merely closes the parent descriptor: surviving analysis processes retain their inherited lease, so another server or offline cleanup still cannot overlap them.

Deleted raw inputs are not recoverable through the server and are not retained for re-analysis after future algorithm changes. Back up the runtime root separately if that capability is required.
