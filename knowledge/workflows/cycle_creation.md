# Cycle creation workflow

1. **Entry:** browser `/cycles/` or operator `POST /api/v1/cycles`.
2. **Call chain:** router -> `CycleRoutesMixin` -> `GitLabMRClient` -> URL match in `ProjectRegistry` -> `CoverageStore.create_or_get_cycle`; legacy complete JSON is accepted only for a registered project.
3. **Persistence:** `coverage_cycles` through `storage/cycles.py`; no GitLab payload or credential is persisted.
4. **Background trigger:** none.
5. **Success:** HTTP 201 created or HTTP 200 reused by `project + mr_iid`; equal IIDs in different projects remain independent.
6. **Failure:** URL/upstream/field/storage errors are mapped to stable codes before any partial Cycle is created.

Tests: `tests/integrations/test_gitlab_mr.py`, `tests/server/test_usability.py`. Contract: `docs/api.md`.
