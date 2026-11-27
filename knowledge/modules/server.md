# Server module

## Responsibility

Assemble dependencies, own HTTP/scheduler lifecycle, match stable routes, resolve immutable principals from Bearer or HttpOnly Session credentials, enforce role/scope with an optional anonymous read-only bypass for the fixed extension report/resolver routes, parse bounded requests, convert domain errors, and write structured diagnostics.

## Non-responsibility

No SQL, artifact persistence, LLVM, Git diff, cumulative mapping, page business-data access, or client-controlled command/path execution.

## Entry files and key types

- `ioscoverage/server/deployment.py`: strict source-side `config/server.json` loader; it owns the runtime-root pointer and the `anonymous_read` flag.
- `ioscoverage/server/termination.py`: main-thread TERM/INT intent installed before runtime acquisition or listener creation; signals only set intent during construction and teardown. HTTP `service_actions` observes intent at a serving safe-point.
- `ioscoverage/server/bootstrap.py`: `RuntimePaths`/ProjectRegistry dependency assembly, option parsing, `create_server`, `main`.
- `ioscoverage/server/mr_coverage_resolver.py`: read-only GitLab MR/Cycle/state/report/Build identity orchestration and versioned MR presentation orchestration. Schema 1 keeps the exact three-SHA gate; schema 2 verifies the page-selected version and delegates display-only line mapping to analysis. The extension entrypoint converts only request-owned non-strict summary/file descriptors to its existing `measurable:0` and null-percentage contract; Cycle summary/details retain the display ratio from the same immutable cache and presentation ID. Coverage reads accept opened, closed and merged MRs with validated refs; Cycle submission and repository synchronization retain their opened-only requirement. The Cycle-scoped read entrypoint discovers the latest registered MR version server-side and shares the same verification, projection and cache; it never updates Cycle/Build/Run or formal reports. Without a successful report, its Cycle-only `diff_only` branch issues a verified source snapshot with no coverage evidence; extension entrypoints retain their original report prerequisite.
- `ioscoverage/server/repository_sync.py`: server-owned serial worker for startup, periodic and requested MR source/target synchronization. It reads Cycle/MR identities, delegates bounded Git writes, and supplies missing-object Build repair without changing Cycle head or scheduling analysis. See `workflows/repository_sync.md`.
- `ioscoverage/server/config.py`: `ProbeConfig` (including `anonymous_read`, AccountStore, and packaging Token), deployment options, and protocol limits.
- `ioscoverage/server/http/server.py`: `ProbeHTTPServer`, analysis-worker, and cleanup-worker lifecycle.
- `ioscoverage/server/http/router.py`: `ProbeRequestHandler` and route matching.
- `auth.py`, `request.py`, `multipart.py`, `response.py`, `errors.py`, `request_logging.py`: cross-route HTTP concerns. `auth.py` resolves anonymous/operator/packaging/run_upload/web_user principals; an invalid Bearer never falls back to Cookie. `_authorize_read` keeps the fixed extension routes anonymous only when configured.
- `http/run_body.py`: Run-only identity/gzip exact-length decoder; bounded temporary-file output, one gzip member, no capability negotiation. Auth runs before decoding. Multipart receives the decoded stream and length; storage and SHA/idempotency remain based on raw profile bytes.
- `http/routes/`: resource-specific handlers, including account/admin routes and admin/operator maintenance conversion.
- `http/routes/pages.py`: keeps `/login/` and `/cycles/` as data-free shells, with Session-aware temporary redirects between authentication and workbench; machine Bearer credentials never open the workbench.
- `http/routes/integrations.py`: strict read-only MR resolver protocol conversion.
- `ioscoverage/server/logging.py`: redacted JSON-line logging.

## Inputs, outputs, dependencies

Inputs are HTTP bytes plus the runtime root loaded from `config/server.json`. Outputs are unchanged JSON/HTML/redirect responses, durable storage calls, scheduler requests, and safe logs. Bootstrap loads projects once; HTTP cannot select repositories or GitLab endpoints.

SIGTERM and Control-C are deferred throughout construction and teardown. Startup cleanup finishes its protected operation before pending termination prevents workers from starting or HTTP serving. Partial constructor failures unwind every owner; teardown attempts all owners even if another fails. The listener may be discoverable during cleanup, but the deferred handlers are already installed. While serving, the HTTP loop observes termination intent at `service_actions`. Both signals enter the same coordinated server shutdown: stop cleanup, reject new analysis work, and terminate any active production analysis process group before joining the scheduler worker. The listener then closes, and cross-process runtime ownership is released only after the worker can no longer access runtime data. The `server_stopped` event is emitted only after successful owner shutdown. Failure to confirm termination is a bounded shutdown error and does not authorize releasing the runtime lock. A crash or SIGKILL is covered by the runtime-lock lease inherited by the surviving analysis execution. Cleanup waits can be cancelled before deletion; a quarantined deletion is joined to completion.

## Common changes and tests

Change routing only in `router.py`; page Session decisions in `routes/pages.py`; resource protocol conversion in the matching route file; principal resolution, Session same-origin checks, and anonymous-read bypass in `auth.py`; parsing/limits in request/multipart; lifecycle in server/bootstrap. Browser writes use Session plus exact Origin/Host. The MR resolver owns cross-read status precedence but delegates GitLab, SQLite, state, report integrity, and Build reads to their existing owners. Run `tests/server/`, `tests/web/`, and `tests/test_architecture.py`.
