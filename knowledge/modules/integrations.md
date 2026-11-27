# Integrations module

## Responsibility

- `git_repository.py`: read fixed blobs and deterministic diffs, inspect literal-path competing rename pairs, and validate one registry-selected repository, commit objects, and declared merge-base through argv-only Git calls.
- `git_sync.py`: the separately owned, mutable Git boundary for MR repository synchronization. It verifies `origin` against the registered GitLab host/project, fetches explicit source/target branches into private refs, and verifies/pins fixed MR and requested Build commits. It never touches the worktree, index, ordinary branches or remote-tracking refs; no pruning, tags, submodules or automatic maintenance. Noninteractive commands run in cancellable process groups and inherit the runtime lease. See `workflows/repository_sync.md`.
- `gitlab_mr.py`: map an HTTPS MR URL through ProjectRegistry and perform one redirect-disabled project-fixed GET using the local PAT. It accepts only detail, `/diffs`, and `/commits` pages, with or without GitLab's `/-/` separator, and returns validated base/start/head refs. `fetch` requires an opened MR by default for Cycle submission and repository synchronization; only the coverage resolver opts into `allow_closed=True` to read existing coverage for closed/merged MRs. Identity and complete diff-ref validation remain mandatory in either mode. The MR display protocol also reads the fixed registered MR `versions?per_page=1` metadata to bind the page-selected diff ID; a different latest ID is rejected. `fetch_latest_version` exposes the same validated ID/refs for the standalone Cycle page, without accepting a caller-selected API path. No full diff payload or arbitrary API path is accepted.
- `xcode_artifacts.py`: use fixed Xcode 26.3 tools to validate Mach-O/dSYM UUIDs and safely inspect dSYM ZIPs.

## Non-responsibility

No unregistered host/repository/command, GitLab write, storage transaction, HTTP response, or analysis policy. Only the dedicated synchronizer may fetch/write private refs; `GitRepository` and every analysis caller remain read-only.

Callers supply already bounded identities. Outputs are validated immutable metadata or stable integration errors. Tests live in `tests/integrations/`, `tests/server/test_api_contracts.py`, and analyzer tests.
