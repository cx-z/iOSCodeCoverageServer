# Architecture

## Package structure

`ioscoverage/` is source code. `<runtime_root>/analysis/` is immutable runtime output and is unrelated to `ioscoverage/analysis/`.

```text
ioscoverage/
  runtime.py                   one-root mutable path contract
  projects.py                  immutable trusted project registry
  server/                      dependency assembly and HTTP boundary
    bootstrap.py
    config.py
    logging.py
    http/{server,router,auth,request,multipart,response,errors}.py
    http/routes/*.py
  web/                         data-free browser rendering
    assets.py
    pages/*.py
    templates/*.html
    static/*.{css,js}
  storage/                     SQLite and immutable file persistence
    coverage_store.py          Cycle/Build/Run facade
    {database,schema,accounts,cycles,builds,runs,artifact_files,validation}.py
    analysis_state.py
    reports.py
    retained_evidence.py
    tokens.py
  analysis/                    offline and background analysis implementation
    scheduler.py
    orchestrator.py
    command_runner.py
    single_build/analyzer.py
    diff/{analyzer,git_diff_parser}.py
    cumulative/analyzer.py
    mapping/{coverage_export,source_region}.py
  integrations/                fixed external-tool boundaries
  cli/                         diagnostic CLI presentation
  lifecycle/                   runtime lock, safe cleanup, and in-process cleanup timer
```

`server.py`, the four root analysis scripts, `cleanup_runtime_data.py`, and `manage_users.py` are thin compatibility entry points. `start_coverage_server.sh` delegates to `scripts/start_coverage_server.sh`.

## Dependency direction

- `server.bootstrap` composes configuration, storage, integrations, scheduler, report store, and HTTP server.
- `server.http` converts requests and responses. It may call storage facades, the scheduler, fixed integration clients, and web renderers; it does not implement SQL, LLVM, or Git algorithms.
- `server.mr_coverage_resolver` is a read-only application service that combines one registry-bounded GitLab MR, an existing Cycle, analysis state, a completion-verified report, and its immutable current Build. It never calls mutation or scheduling APIs. Its schema-2 path owns bounded immutable presentation caching and calls `analysis.mr_presentation` for fixed-object display mapping; formal report storage and analyzers retain their existing semantics.
- `web` accepts already projected values and trusted internal template names. It must not depend on SQLite, TokenStore, Build artifacts, Runs, or arbitrary paths.
- `storage.coverage_store` is the stable facade over Cycle/Build/Run mixins; `storage.accounts.AccountStore` independently owns users and Sessions. HTTP code never opens SQLite directly.
- `analysis` reads immutable database/file identities and fixed Git objects. The scheduler invokes the orchestrator, which reuses the single-Build, diff, and cumulative analyzers.
- `integrations` owns subprocess/network boundaries for project-selected Git, Xcode artifacts, and registered GitLab MR GET.
- `cli` validates command arguments and formats results; it does not duplicate analysis logic.
- `lifecycle` verifies the latest report graph, coordinates the server process lock, publishes retained same-Build evidence, and removes only proven redundant runtime paths.

No lower layer imports an HTTP route. Storage does not depend on Web. Web does not depend on storage. Analysis never depends on a browser renderer.

## Service assembly and lifecycle

`ioscoverage.server.deployment` loads the single runtime-root pointer from source-side `config/server.json`. `ioscoverage.server.bootstrap.main` then loads ProjectRegistry and persistent tokens, configures append-only logging, and calls `create_server`. `create_server` initializes the compatible fresh SQLite schema and composes storage, analysis, reports, and HTTP.

`ProbeHTTPServer` owns analysis-scheduler and cleanup-manager start/shutdown. Request threads share immutable dependencies but not SQLite connections. The scheduler owns one generation worker, while each production analysis attempt runs the unchanged orchestrator and all of its LLVM/Git descendants in a dedicated child process group. The cleanup manager owns one startup/periodic/manual maintenance worker. The production process holds `state/server.lock`, and the active analysis execution inherits the same lease through every controlled subprocess boundary. Online cleanup verifies that exact ownership, pauses analysis and waits idle, then takes CoverageStore's mutation guard; it never stops the HTTP listener. Shutdown can cancel either pre-deletion wait, but it never interrupts a cleanup after the destructive guard is acquired. SIGTERM enters coordinated shutdown, which terminates an active analysis process group, joins the scheduler within a bounded grace period, closes the socket, and only then permits bootstrap to release `server.lock`; inability to confirm termination is a shutdown error, not permission to release ownership. On a crash or SIGKILL, surviving analysis descendants retain the lease until they exit. Report loading retries one newer latest-success identity when concurrent cleanup removes its stale immutable selection. Offline `--apply` still requires exclusive ownership and therefore requires service shutdown.

## Runtime versus source

The following live only below the explicit runtime root and must never be derived from source location:

- `state/`: operator, packaging, restricted Run upload, GitLab credential files, and runtime lock;
- `instance/`: SQLite database;
- `data/`: Build artifacts, Run payloads, probe uploads, staging;
- `analysis/`: immutable analysis results and failure evidence;
- `logs/`: append-only structured service log.

`config/server.json` only selects the root; `RuntimePaths` remains the only mutable path authority. Repository selection is `Cycle.project -> ProjectRegistry -> ProjectDefinition -> GitRepository`; diagnostic CLIs accept runtime root and immutable identities, never a repository path.

Real project identities, repository paths, credentials, uploads, databases, logs, and analysis output belong exclusively to the external runtime root. Public source, examples, tests, and knowledge documents use fictional identities. The anchored ignore policy protects accidental root-local runtime copies without hiding `ioscoverage/analysis/` source.

`storage.build_evidence.BuildEvidenceStore` owns the small atomic per-Build pointers published by background orchestration when formal current-Build selection cannot succeed. MR report readers consume those fixed references without mutation, and lifecycle cleanup validates/protects their source outputs. No new SQLite schema, profile merge scope or formal report success semantics are introduced.
