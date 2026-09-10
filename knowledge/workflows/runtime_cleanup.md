# Runtime cleanup workflow

## Entry

`cleanup_runtime_data.py` delegates to `ioscoverage.cli.cleanup_runtime_data`, loads the same configured RuntimePaths as the server, and calls `RuntimeDataCleaner`. Dry-run is the default; only `--apply` deletes.

## Call chain

```text
CLI -> ServerDeploymentConfiguration -> RuntimeDataCleaner
  -> RuntimeProcessLock before planning (apply only)
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

The only new persistent data is completion-protected evidence below `analysis/retained-build`. Evidence is bound to the Cycle, Build, input set, and exact Run identities. Publication and retry fsync the complete directory-entry chain before evidence can authorize deletion. Superseded valid evidence is removed with its obsolete single-Build result. No SQLite row is added or changed. The server and each offline analysis CLI hold `state/server.lock`; the file contains no credential.

## Success and failure

Success emits JSON counts and byte totals. Apply holds the runtime lock for snapshot, plan, publication, and deletion, and publishes durable evidence before removing incorporated profiles, non-coverage artifacts, or old complete generations. Incorporated Runs must still match their SQLite ownership, state, payload, and metadata identities. Raw profiles and removable Build artifacts must match the size/SHA-256 incorporated into the successful result while planning and around evidence publication. Directory candidates also bind their complete relative tree and file identities.

Final deletion traverses from one opened runtime-root descriptor with `O_NOFOLLOW`. It atomically renames each candidate into an invocation-private quarantine below `analysis`, revalidates the isolated file or complete tree, and only then removes it. A concurrently replaced entry is retained in quarantine or remains at its original name, and an ancestor-path swap cannot redirect deletion outside the runtime root. Superseded evidence is quarantined before its obsolete source result. A missing latest report, wrong result kind, database drift, candidate-byte drift, invalid completion, ambiguous reference, queued/running task, held service lock, path escape, symlink, or publication failure causes conservative retention or a bounded formal-cleanup error. The last report and existing business metadata remain untouched.

## Related code and tests

- `ioscoverage/lifecycle/cleanup.py`
- `ioscoverage/lifecycle/runtime_lock.py`
- `ioscoverage/lifecycle/secure_delete.py`
- `ioscoverage/storage/retained_evidence.py`
- `ioscoverage/analysis/single_build/analyzer.py`
- `tests/lifecycle/test_runtime_cleanup.py`
- `tests/analysis/test_single_build.py`
