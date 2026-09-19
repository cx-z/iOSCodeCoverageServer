# Cycle creation workflow

1. **Entry:** authenticated developer/admin browser `/cycles/` or operator compatibility `POST /api/v1/cycles`.
2. **Call chain:** router -> Principal scope -> `CycleRoutesMixin` -> `GitLabMRClient` -> URL match in `ProjectRegistry` -> `CoverageStore.create_or_get_cycle_for_user`; operator legacy complete JSON remains compatible for a registered project.
3. **Persistence:** Web submission writes `coverage_cycles` and `cycle_submitters` in one transaction. No GitLab payload, password, Session material, or credential is persisted with the Cycle.
4. **Background trigger:** none.
5. **Success:** HTTP 201 created or HTTP 200 reused by `project + mr_iid`; Web responses include `submission_status=added|existing`. Different users share one Cycle but receive independent relations; equal IIDs in different projects remain independent.
6. **Failure:** URL/upstream/field/storage errors are mapped to stable codes before any partial Cycle is created.

Developer list/detail reads require the current relation; admin sees all Cycles with submitter usernames; operator sees all without creating relations. Historical relation-free Cycles remain admin/operator-only until a developer submits that MR.

Tests: `tests/storage/test_cycle_submitters.py`, `tests/server/test_multi_user_auth.py`, `tests/integrations/test_gitlab_mr.py`. Contract: `docs/api.md`.
