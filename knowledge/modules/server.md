# Server module

## Responsibility

Assemble dependencies, own HTTP/scheduler lifecycle, match stable routes, enforce Token scope, parse bounded requests, convert domain errors, and write structured diagnostics.

## Non-responsibility

No SQL, artifact persistence, LLVM, Git diff, cumulative mapping, page business-data access, or client-controlled command/path execution.

## Entry files and key types

- `ioscoverage/server/deployment.py`: strict source-side `config/server.json` loader; it owns only the runtime-root pointer.
- `ioscoverage/server/bootstrap.py`: `RuntimePaths`/ProjectRegistry dependency assembly, option parsing, `create_server`, `main`.
- `ioscoverage/server/config.py`: `ProbeConfig` and protocol limits.
- `ioscoverage/server/http/server.py`: `ProbeHTTPServer` lifecycle.
- `ioscoverage/server/http/router.py`: `ProbeRequestHandler` and route matching.
- `auth.py`, `request.py`, `multipart.py`, `response.py`, `errors.py`, `request_logging.py`: cross-route HTTP concerns.
- `http/routes/`: resource-specific handlers.
- `ioscoverage/server/logging.py`: redacted JSON-line logging.

## Inputs, outputs, dependencies

Inputs are HTTP bytes plus the runtime root loaded from `config/server.json`. Outputs are unchanged JSON/HTML/redirect responses, durable storage calls, scheduler requests, and safe logs. Bootstrap loads projects once; HTTP cannot select repositories or GitLab endpoints.

## Common changes and tests

Change routing only in `router.py`; resource protocol conversion in the matching route file; authentication in `auth.py`; parsing/limits in request/multipart; lifecycle in server/bootstrap. Run `tests/server/`, `tests/web/test_report_ui.py`, and `tests/test_architecture.py`.
