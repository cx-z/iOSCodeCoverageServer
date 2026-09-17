# Server module

## Responsibility

Assemble dependencies, own HTTP/scheduler lifecycle, match stable routes, enforce Token scope with an optional anonymous read-only bypass for the fixed extension report/resolver routes, parse bounded requests, convert domain errors, and write structured diagnostics.

## Non-responsibility

No SQL, artifact persistence, LLVM, Git diff, cumulative mapping, page business-data access, or client-controlled command/path execution.

## Entry files and key types

- `ioscoverage/server/deployment.py`: strict source-side `config/server.json` loader; it owns the runtime-root pointer and the `anonymous_read` flag.
- `ioscoverage/server/bootstrap.py`: `RuntimePaths`/ProjectRegistry dependency assembly, option parsing, `create_server`, `main`.
- `ioscoverage/server/mr_coverage_resolver.py`: read-only GitLab MR/Cycle/state/report/Build identity orchestration and exact three-SHA annotation decision.
- `ioscoverage/server/config.py`: `ProbeConfig` (including `anonymous_read`), deployment options, and protocol limits.
- `ioscoverage/server/http/server.py`: `ProbeHTTPServer`, analysis-worker, and cleanup-worker lifecycle.
- `ioscoverage/server/http/router.py`: `ProbeRequestHandler` and route matching.
- `auth.py`, `request.py`, `multipart.py`, `response.py`, `errors.py`, `request_logging.py`: cross-route HTTP concerns. `auth.py` exposes `_authorize_read`: when `anonymous_read` is enabled it lets the fixed read-only extension routes through without a credential; otherwise it falls back to `_require_token`.
- `http/routes/`: resource-specific handlers, including operator-only maintenance status/trigger conversion.
- `http/routes/integrations.py`: strict read-only MR resolver protocol conversion.
- `ioscoverage/server/logging.py`: redacted JSON-line logging.

## Inputs, outputs, dependencies

Inputs are HTTP bytes plus the runtime root loaded from `config/server.json`. Outputs are unchanged JSON/HTML/redirect responses, durable storage calls, scheduler requests, and safe logs. Bootstrap loads projects once; HTTP cannot select repositories or GitLab endpoints.

SIGTERM and Control-C enter the same coordinated server shutdown: stop cleanup, reject new analysis work, and terminate any active production analysis process group before joining the scheduler worker. The listener then closes, and cross-process runtime ownership is released only after the worker can no longer access runtime data. Failure to confirm termination is a bounded shutdown error and does not authorize releasing the runtime lock. A crash or SIGKILL is covered by the runtime-lock lease inherited by the surviving analysis execution. Cleanup waits can be cancelled before deletion; a quarantined deletion is joined to completion.

## Common changes and tests

Change routing only in `router.py`; resource protocol conversion in the matching route file; authentication and the anonymous-read bypass in `auth.py` (`_authorize_read` on the resolver/report/file routes); parsing/limits in request/multipart; lifecycle in server/bootstrap. The MR resolver owns cross-read status precedence but delegates GitLab, SQLite, state, report integrity, and Build reads to their existing owners. Run `tests/server/`, `tests/web/test_report_ui.py`, and `tests/test_architecture.py`.
