# Build registration workflow

1. **Entry:** packaging Token or operator compatibility `POST /api/v1/builds` multipart.
2. **Call chain:** router -> `BuildRoutesMixin` -> bounded multipart parser -> `CoverageStore.register_build` -> Cycle.project -> ProjectRegistry repository/Bundle allowlist -> Git and Xcode validators.
3. **Persistence:** manifest and artifacts stage under `data/.staging`, then publish using generic logical-role filenames; original safe upload names stay in the manifest.
4. **Background trigger:** only when this new Build actually promotes one or more pending Runs.
5. **Success:** created or idempotent replay response with the existing fields.
6. **Failure:** set/size/SHA/Git/UUID/ZIP/identity failures publish no partial Build; a replay cannot replace Build identity.

Tests: `tests/server/test_api_contracts.py`, `tests/server/test_automatic_analysis.py`. Contract: `docs/api.md`.

coveragectl normally receives a known `cycle_id` plus packaging Token. That scope can GET/PATCH the known Cycle and POST/GET Build only; it cannot enumerate/create Cycles, manage accounts, retry analysis, or trigger cleanup. `--mr-url` remains only for an explicit operator compatibility credential.
