# iOS Coverage Platform Server

This repository contains a standalone, Mac-local server for incremental code coverage from physical iOS devices. It accepts LLVM artifacts from registered Builds and user-initiated `.profraw` uploads, analyzes every Build independently, and conservatively accumulates source-region coverage over the current Build's immutable Git diff.

## Deployment

The source tree does not store runtime data. For the initial setup, configure the runtime data location and prepare the project registry:

```bash
cp config/server.example.json config/server.json
# Edit config/server.json and set runtime_root to an absolute data directory.

mkdir -p <runtime_root>/config
cp config/projects.example.json <runtime_root>/config/projects.json

./start_coverage_server.sh
```

`config/server.json` is machine-local configuration and is ignored by Git. If `runtime_root` is empty, missing, or not an absolute path, the server stops with a configuration error before creating tokens, a database, or logs. The repository's `projects.example.json` contains fictional projects only; the real project registry must exist only at `<runtime_root>/config/projects.json`.

All mutable data lives under that runtime root:

```text
config/projects.json
state/gitlab_api_token
state/operator_token
state/run_upload_token
instance/coverage.sqlite3
data/
analysis/
logs/server.log
```

Missing operator and Run tokens are generated automatically with `0600` permissions. The GitLab token must be provisioned separately by the operator; if it is absent, only MR URL-based Cycle creation is unavailable.

Open `http://127.0.0.1:8765/cycles/` in a browser. See [docs/api.md](docs/api.md) for the HTTP protocol, [docs/daily_workflow.md](docs/daily_workflow.md) for the daily workflow, and [docs/runtime_deployment.md](docs/runtime_deployment.md) and [docs/project_registry.md](docs/project_registry.md) for deployment configuration.

## Optional MR Extension

Install the optional Chrome extension to view line-level coverage directly on GitLab Merge Request diff pages. The extension uses the local server's read-only endpoint to validate the MR diff identity and retrieve the coverage report. The server's Cycle, Build, Run, background analysis, and built-in report features work without the extension.

- Repository: [IOSCoverageGitLabExtension](https://github.com/cx-z/IOSCoverageGitLabExtension)
- Download: [v0.2.1 Release](https://github.com/cx-z/IOSCoverageGitLabExtension/releases/tag/v0.2.1)

## Diagnostic and maintenance CLI

The four analysis compatibility entry points require an explicit `--runtime-root` and accept only immutable identities such as Cycle, Build, and Run IDs. They do not accept repository or artifact paths:

```bash
python3 analyze_cycle_latest.py --runtime-root /absolute/runtime --cycle-id <cycle-id>
```

When the server is running, a Run upload asynchronously triggers the same orchestrator; the CLI does not need to be invoked manually.

Runtime cleanup uses the `runtime_root` already configured in `config/server.json` and is a dry-run unless `--apply` is explicitly supplied. Formal cleanup requires the server to be stopped:

```bash
python3 cleanup_runtime_data.py
python3 cleanup_runtime_data.py --apply
```

See [docs/runtime_data_lifecycle.md](docs/runtime_data_lifecycle.md) for retained evidence, deletion rules, and safety boundaries.

## Development Verification

```bash
PYTHONPYCACHEPREFIX=/private/tmp/ios-coverage-tests-pycache \
python3 -m unittest discover -s tests -t . -v
```

Start codebase navigation at [knowledge/README.md](knowledge/README.md). The server's Python package is named `ioscoverage`.

## Public Repository Check

Run this command before committing or publishing:

```bash
python3 scripts/check_public_repository.py
```

The checker scans tracked files and untracked files that are not excluded by `.gitignore`. It rejects internal project identifiers, local user directories, runtime artifacts, and common plaintext credential patterns. It reports only relative file paths and safe finding categories, never suspected credential contents. Real `projects.json` files, tokens, SQLite databases, profiles, logs, and analysis results are deployment data and must not enter the source repository.

## License

This project is licensed under the [Apache License 2.0](LICENSE).
