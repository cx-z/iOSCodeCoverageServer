# Engineering invariants

These rules are ordinary-task stop conditions. A change that needs to violate one requires an explicit product/design decision, protocol or migration plan, and dedicated review.

## Identity and persistence

- Cycle ID and `base_sha` are immutable; `head_sha` may advance under the existing API.
- A Build is immutable after registration, including Git snapshot, `coverage_binary`, manifest, UUIDs, hashes, sizes, and actual artifact set.
- A Run remains tied to the Cycle/Build that created it. A complete unknown-Build Run may remain `pending_build` and later be promoted.
- Idempotent replay succeeds only for the same stable identity and content; conflicting content is rejected.
- Uploads use staging, size/SHA validation, fsync, and atomic publication before success.
- Existing SQLite schema, triggers, meanings, and runtime paths are not silently migrated or reinterpreted.
- `project + mr_iid` identifies one Cycle when IID is present; equal IIDs in different projects remain independent.
- Build and Run resolve project only through their Cycle.

## Analysis

- Each Build independently validates and merges only its own Runs.
- `.profraw` or `.profdata` from different Builds are never directly merged.
- The current denominator is the current Build's fixed `merge_base_sha -> git_sha`, never Cycle `base_sha` or a mutable branch tip.
- Git source comes from exact commit objects, not the working tree. Analysis never checks out, resets, cleans, stashes, merges, rebases, fetches, or creates worktrees.
- Every Git operation uses the repository registered for Cycle.project. Missing projects fail without fallback.
- Current positive region evidence wins. Historical positive evidence is inherited only when path/rename, Git line mapping, region range, function identity, and exact source bytes prove the region unchanged.
- Modified, new, copied, ambiguous, or corrupt regions do not inherit. Missing inheritance is uncovered, not guessed covered.
- `not_measurable`, `unknown`, and deleted lines do not enter the measurable coverage denominator.
- Analysis failure keeps durable Runs and the previous latest success.
- Immutable analysis output is reusable only after its completion manifest and file identities validate.

## Scheduling and security

- Run upload success does not wait for LLVM and is not reversed by later analysis failure.
- One global worker runs one Cycle analysis at a time; generations coalesce new input without letting stale completion overwrite newer state.
- Operator, restricted Run upload, and GitLab credentials remain separate.
- Restricted Run credentials can only submit probes and Runs. GitLab credentials are only for registry-matched HTTPS MR GETs.
- All runtime files derive from one explicit RuntimePaths root; requests cannot override paths, repositories, GitLab addresses, or Bundle allowlists.
- Tokens, Authorization headers, multipart bodies, and secrets never enter logs, responses, HTML, SQLite, analysis evidence, or documentation.
- Callers cannot supply arbitrary repository, command, Git ref, artifact, report, or output paths.
- Public source and examples contain only fictional project identities. Real ProjectRegistry data, credentials, SQLite files, profiles, logs, and analysis output remain untracked deployment data.
- A publication candidate must pass `scripts/check_public_repository.py`, which scans tracked and non-ignored untracked files without printing matched secret bytes.
