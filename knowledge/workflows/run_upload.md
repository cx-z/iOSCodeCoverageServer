# Run upload workflow

1. **Entry:** operator or restricted iOS Token on `POST /api/v1/runs`.
2. **Call chain:** router -> Run auth scope -> `RunRoutesMixin` -> `decoded_run_body` (identity/gzip) -> bounded multipart parser -> `CoverageStore.save_run`.
   The Run boundary bounds both encoded and decoded multipart bytes, validates gzip framing/CRC and closes temporary files. It accepts only one gzip member and rejects trailing data, transfer coding and ambiguous lengths. The parser uses decoded length. New SDKs always gzip in every network environment; identity remains accepted for existing clients. Deploy the local server first; `/health` is unchanged.
3. **Persistence:** metadata/profile/SHA stage, validate raw/indexed format and bounded canonical provenance, fsync, and atomically publish under the original Cycle/Run directory; metadata enters `runs`.
4. **Background trigger:** a newly created uploaded/analyzable Run requests its Cycle generation after persistence. Pending Runs and exact replays do not trigger.
5. **Success:** HTTP 200/201/202 means durable receipt and does not wait for LLVM. Indexed objects additionally echo indexed format and canonical metadata SHA; raw responses remain unchanged. The client must durably commit its strict receipt before completing source obligations.
6. **Failure:** request/identity/integrity errors leave no successful Run; later analysis failure never reverses an accepted Run.
7. **After compaction:** an identical replay still validates the submitted bytes and succeeds only when retained Build evidence proves the missing server-side payload was intentionally incorporated. An unexplained missing payload remains an integrity error.

Tests: `tests/server/test_probes.py`, `test_api_contracts.py`, `test_automatic_analysis.py`. Contract: `docs/api.md`.
