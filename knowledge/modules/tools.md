# Tools and CLI module

## Responsibility

Expose stable operator commands while keeping implementation in the package.

- Root `server.py` delegates to `ioscoverage.server.bootstrap`; service startup reads `config/server.json` and takes no runtime-root argument.
- Root `analyze_*.py` delegates to the matching `ioscoverage.cli` module.
- Root `cleanup_runtime_data.py` delegates to the offline lifecycle CLI. It reads `runtime_root` from `config/server.json`, defaults to dry-run, and requires a stopped server for `--apply`. The running service exposes admin/operator maintenance GET/POST for status and immediate asynchronous cleanup.
- Root `manage_users.py` delegates to `ioscoverage.cli.manage_users`; `create-admin --username` reads two hidden password prompts, never accepts a password argument, and never prints password storage material.
- Root `start_coverage_server.sh` delegates to `scripts/start_coverage_server.sh` and is the zero-argument daily entry. It force-compiles server Python sources and validates append access to both configured structured and console logs before stopping an existing instance, then starts the current source in an independent session/process group with detached stdin/stdout/stderr. It waits up to 60 seconds for its own child to own port 8765 and answer a proxy-bypassed local health request before returning success. Process inspection uses bounded subprocess timeouts; optional LAN URL discovery is also bounded. Compilation/configuration/log-open failure leaves the old service untouched. Runtime stdout/stderr append to `<runtime_root>/logs/server-console.log`; structured logs remain in `server.log`. Early exit fails startup; readiness timeout reports the still-background PID without forcibly interrupting startup cleanup.
- Root `stop_coverage_server.sh` delegates to `scripts/stop_coverage_server.py`: inspect the fixed daily port 8765, read exact Darwin KERN_PROCARGS2 argv boundaries (never shell-split ps display text), require a Python executable and a narrow option allowlist before this checkout’s exact `server.py` (cwd-aware for relative invocations), read `ps` birth/command fields under `LC_ALL=C` so terminal date localization cannot shift command parsing, recheck that identity, send SIGTERM once and wait up to 60 seconds. Attached/combined execution options and unknown interpreter flags fail closed; native argument inspection errors also refuse signalling. An unrelated listener is refused. No listener is an idempotent success; timeout never sends SIGKILL or deletes runtime locks. Alternate-port diagnostic services are outside these daily scripts.
- `ioscoverage/cli/` owns argument parsing, exit codes, and JSON presentation only; every analyzer accepts `--runtime-root` plus immutable IDs.
- `scripts/check_public_repository.py` owns the pre-publication scan of tracked and non-ignored untracked files. It reports only relative paths and finding categories.

## Non-responsibility

Wrappers do not implement HTTP, persistence, LLVM, diff, or cumulative behavior and do not modify `sys.path`. CLI arguments remain restricted identities, never arbitrary repository, artifact, command, or output paths.

Use `tests/test_server_control.py` for compile-before-stop, ownership and graceful-stop safety; `tests/test_architecture.py` for import/help/path compatibility, `tests/test_public_repository_check.py` for publication safety, `tests/lifecycle/` for cleanup safety, and the matching analyzer tests for behavior.
