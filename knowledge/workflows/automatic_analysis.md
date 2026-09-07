# Automatic analysis workflow

1. **Entry:** valid Run, pending-Run promotion, or operator retry.
2. **Call chain:** route -> `AnalysisScheduler.request_analysis` -> `AnalysisStateStore` generation -> worker claim -> `CycleAnalysisOrchestrator.analyze`.
3. **Persistence:** generation and small latest-success references in SQLite; immutable stage outputs under root `analysis/`.
4. **Background behavior:** one global worker; new generations arriving during a run coalesce into one subsequent latest snapshot.
5. **Success:** state points to a completion-validated orchestration and Cycle result. The report API reads that latest success.
6. **Failure:** stable stage/code/message are saved, the Run and old latest success remain, and there is no unbounded retry.

The orchestrator snapshots all Build/Run rows, selects current by Cycle `head_sha`, resolves the Cycle project's registered repository, independently analyzes each Build, creates current diff, and accumulates source regions. Tests: `tests/server/test_automatic_analysis.py`, `tests/analysis/test_orchestrator.py`. Guide: `docs/automatic_analysis.md`.
