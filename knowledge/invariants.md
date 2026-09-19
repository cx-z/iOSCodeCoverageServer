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
- A developer can enumerate or read a Cycle only after that platform account submitted the MR through Web. Multiple submitters share the same Cycle; legacy relation-free Cycles remain admin/operator-only until submission establishes a relation.
- User creation/status/password reset and Session revocation use short transactions. SQLite stores salted password material and Session SHA-256 only, never plaintext passwords or raw Session Tokens.
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
- Incorporated raw profiles may be removed only after a crash-durable retained baseline binds the exact Cycle, Build, input set, and Run identities to an intact `merged.profdata` and LLVM export. Existing evidence must be re-fsynced before it can authorize retry deletion. A later Run merges with that baseline only inside the same Build.
- The exact coverage binary remains available for future same-Build exports. Historical inheritance continues to consume independently produced per-Build exports and the existing strict Git/region proof.

## Scheduling and security

- Run upload success does not wait for LLVM and is not reversed by later analysis failure.
- One global worker runs one Cycle analysis at a time; generations coalesce new input without letting stale completion overwrite newer state.
- Online cleanup runs only while the server owns `server.lock`, the analysis worker is paused and idle, and the Cycle/Build/Run mutation guard is held. Queued generations are preserved; a running generation blocks deletion. HTTP serving remains active, and a report read whose stale latest-success generation disappears retries only the newly persisted immutable identity once.
- Shutdown may cancel cleanup while it waits for analysis or the mutation guard, but cannot interrupt quarantine validation/deletion after that guard is acquired.
- Production background analysis is isolated in a dedicated process group. Scheduler idle, failure persistence, and cleanup eligibility require proof that this entire group is absent. A termination failure keeps both the durable generation and scheduler owner running, blocks replacement analysis and cleanup, and prevents `server.lock` release. The worker and its controlled Git/LLVM/Xcode descendants inherit the same runtime-lock lease, so an unexpected parent-service exit cannot permit a new server or offline cleanup while analysis remains alive. Coordinated SIGTERM shutdown terminates the group and joins the scheduler within a bounded grace period.
- Operator, packaging, restricted Run upload, and GitLab credentials remain pairwise separate; Web users authenticate with independent HttpOnly Sessions.
- Packaging credentials can only read/update a known Cycle and register/review Builds. They cannot list/create Cycles, manage users, retry analysis, or clean runtime data.
- Restricted Run credentials can only submit probes and Runs. GitLab credentials are only for registry-matched HTTPS MR GETs.
- All runtime files derive from one explicit RuntimePaths root; requests cannot override paths, repositories, GitLab addresses, or Bundle allowlists.
- Tokens, Authorization headers, multipart bodies, and secrets never enter logs, responses, HTML, SQLite, analysis evidence, or documentation.
- Callers cannot supply arbitrary repository, command, Git ref, artifact, report, or output paths.
- Public source and examples contain only fictional project identities. Real ProjectRegistry data, credentials, SQLite files, profiles, logs, and analysis output remain untracked deployment data.
- A publication candidate must pass `scripts/check_public_repository.py`, which scans tracked and non-ignored untracked files without printing matched secret bytes.
- Destructive cleanup is dependency-driven, confined to `RuntimePaths.root`, rejects symlinks, and requires continuous ownership of the shared runtime lock from before snapshot/planning through publication/deletion. Final traversal uses no-follow descriptors rooted at one opened runtime directory. Every candidate is atomically isolated into an invocation-private quarantine, then the exact file identity or complete directory-tree identity is revalidated before removal; a prior path or inode check never directly authorizes a later name-based unlink. Latest success is validated by exact result kind, layout, semantic identity, and output bytes. Incorporated Runs must still match SQLite; ambiguous, changed, or unproven data is retained.
