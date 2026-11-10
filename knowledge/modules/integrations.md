# Integrations module

## Responsibility

- `git_repository.py`: read fixed blobs and deterministic diffs, inspect literal-path competing rename pairs, and validate one registry-selected repository, commit objects, and declared merge-base through argv-only Git calls.
- `gitlab_mr.py`: map an HTTPS MR URL through ProjectRegistry and perform one redirect-disabled project-fixed GET using the local PAT. It accepts only detail, `/diffs`, and `/commits` pages, with or without GitLab's `/-/` separator, and returns validated base/start/head refs. The MR display protocol also reads the fixed registered MR `versions?per_page=1` metadata to bind the page-selected diff ID; a different latest ID is rejected. No full diff payload or arbitrary API path is accepted.
- `xcode_artifacts.py`: use fixed Xcode 26.3 tools to validate Mach-O/dSYM UUIDs and safely inspect dSYM ZIPs.

## Non-responsibility

No unregistered host/repository/command, Git mutation, GitLab write, storage transaction, HTTP response, or analysis policy.

Callers supply already bounded identities. Outputs are validated immutable metadata or stable integration errors. Tests live in `tests/integrations/`, `tests/server/test_api_contracts.py`, and analyzer tests.
