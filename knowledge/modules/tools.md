# Tools and CLI module

## Responsibility

Expose stable operator commands while keeping implementation in the package.

- Root `server.py` delegates to `ioscoverage.server.bootstrap`; service startup reads `config/server.json` and takes no runtime-root argument.
- Root `analyze_*.py` delegates to the matching `ioscoverage.cli` module.
- Root `start_coverage_server.sh` delegates to `scripts/start_coverage_server.sh` and is the zero-argument daily entry.
- `ioscoverage/cli/` owns argument parsing, exit codes, and JSON presentation only; every analyzer accepts `--runtime-root` plus immutable IDs.
- `scripts/check_public_repository.py` owns the pre-publication scan of tracked and non-ignored untracked files. It reports only relative paths and finding categories.

## Non-responsibility

Wrappers do not implement HTTP, persistence, LLVM, diff, or cumulative behavior and do not modify `sys.path`. CLI arguments remain restricted identities, never arbitrary repository, artifact, command, or output paths.

Use `tests/test_architecture.py` for import/help/path compatibility, `tests/test_public_repository_check.py` for publication safety, and the matching analyzer tests for behavior.
