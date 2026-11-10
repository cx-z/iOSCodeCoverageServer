# Runtime cleanup workflow

## Entry

Production bootstrap creates `RuntimeCleanupManager` around the existing `RuntimeDataCleaner`. After the listen socket binds, it recovers interrupted analysis state to queued, runs one synchronous cleanup before workers or HTTP serving, then starts a 24-hour in-memory timer. Operator GET/POST `/api/v1/maintenance/cleanup` exposes bounded status and asynchronous manual triggering. Restarting the process resets the timer.

`cleanup_runtime_data.py` remains the offline audit/fallback: it loads the same RuntimePaths, defaults to dry-run, and only `--apply` deletes.

## Call chain

```text
server bootstrap -> RuntimeCleanupManager -> RuntimeDataCleaner.run_online
  -> verify the server-owned RuntimeProcessLock
  -> pause AnalysisScheduler and wait idle
  -> CoverageStore maintenance guard
  -> allow queued generations, reject running state
  -> dependency-driven plan/publication/deletion

offline CLI -> ServerDeploymentConfiguration -> RuntimeDataCleaner.run
  -> acquire RuntimeProcessLock before planning (apply only)
  -> read-only SQLite lifecycle snapshot
  -> CoverageReportStore latest-success verification
  -> result-kind/path/identity verification
  -> RetainedBuildEvidenceStore scoped single-Build verification
  -> current SQLite Run and candidate file identity verification
  -> retention set and deletion plan
  -> queued/running state recheck (apply only)
  -> all-file identity preflight (apply only)
  -> publish retained evidence
  -> recheck each file identity and delete exact candidates below runtime_root
```

## Persistence

Independent MR Build references below `analysis/build-evidence` are validated against immutable completion, SQLite Build/Run ownership and on-disk metadata before their source result is protected. These references never authorize additional profile/artifact deletion. An unproven reference conservatively prevents that Build's generation cleanup. The pointer is published under the existing analysis/runtime-lock boundary; cleanup sees a stable writer-free snapshot.

The only cleanup-specific persistent data is completion-protected evidence below `analysis/retained-build`. Timer/status state is intentionally process-local. Evidence is bound to the Cycle, Build, input set, and exact Run identities. Publication and retry fsync the complete directory-entry chain before evidence can authorize deletion. Superseded valid evidence is removed with its obsolete single-Build result. No SQLite row is added or changed. The server and each offline analysis CLI hold `state/server.lock`; the file contains no credential.

## Success and failure

Success records aggregate counts and byte totals. Online POST returns before cleanup runs; repeated requests coalesce. The server keeps listening, while analysis execution and storage mutation are serialized around the destructive window. Apply holds the runtime lock for snapshot, plan, publication, and deletion, and publishes durable evidence before removing incorporated profiles, non-coverage artifacts, or old complete generations. Incorporated Runs must still match their SQLite ownership, state, payload, and metadata identities. Raw profiles and removable Build artifacts must match the size/SHA-256 incorporated into the successful result while planning and around evidence publication. Directory candidates also bind their complete relative tree and file identities.

Process-local status keeps aggregate counters and at most 20 skipped-item samples. `last_trigger` is the active or most recently completed attempt; one coalesced manual follow-up is exposed separately as `pending_trigger`. Shutdown cancels only waits for the analysis-idle and storage-mutation barriers. After the storage guard is acquired, shutdown joins the cleanup so quarantine validation/deletion is not interrupted. Active production analysis is terminated as one owned process group and the scheduler is joined within a bounded grace period; `server.lock` remains owned unless termination is confirmed. Its lease is inherited by analysis subprocesses, preventing a crash or SIGKILL of the server from allowing a replacement service or offline cleanup to overlap surviving analysis. A report reader that captured the previous latest-success identity may reload and validate the newer identity once if cleanup removes the stale generation.

Final deletion traverses from one opened runtime-root descriptor with `O_NOFOLLOW`. It atomically renames each candidate into an invocation-private quarantine below `analysis`, revalidates the isolated file or complete tree, and only then removes it. A concurrently replaced entry is retained in quarantine or remains at its original name, and an ancestor-path swap cannot redirect deletion outside the runtime root. Superseded evidence is quarantined before its obsolete source result. A missing latest report, wrong result kind, database drift, candidate-byte drift, invalid completion, ambiguous reference, running task, uncoordinated queued task, held service lock, path escape, symlink, or publication failure causes conservative retention or a bounded cleanup error. The last report and existing business metadata remain untouched.

## Related code and tests

- `ioscoverage/lifecycle/cleanup.py`
- `ioscoverage/lifecycle/manager.py`
- `ioscoverage/lifecycle/runtime_lock.py`
- `ioscoverage/lifecycle/secure_delete.py`
- `ioscoverage/storage/retained_evidence.py`
- `ioscoverage/analysis/single_build/analyzer.py`
- `tests/lifecycle/test_runtime_cleanup.py`
- `tests/lifecycle/test_runtime_cleanup_manager.py`
- `tests/analysis/test_single_build.py`
