#!/usr/bin/env python3
"""Service dependency assembly for the multi-project coverage platform."""

import argparse
import logging
import secrets
import uuid
from pathlib import Path
from typing import Callable, Optional

from ioscoverage.analysis.orchestrator import CycleAnalysisOrchestrator
from ioscoverage.analysis.scheduler import AnalysisScheduler
from ioscoverage.analysis.single_build.analyzer import AnalysisConfiguration
from ioscoverage.integrations.gitlab_mr import GitLabMRClient
from ioscoverage.integrations.xcode_artifacts import DEFAULT_DEVELOPER_DIR, ArtifactValidationError, XcodeArtifactValidator
from ioscoverage.projects import ProjectRegistry, ProjectRegistryError
from ioscoverage.runtime import RuntimePaths, RuntimeConfigurationError
from ioscoverage.server.config import *
from ioscoverage.server.deployment import (
    ServerDeploymentConfiguration,
    ServerDeploymentConfigurationError,
)
from ioscoverage.server.http.server import ProbeHTTPServer
from ioscoverage.server.logging import configure_logging, write_log
from ioscoverage.storage.analysis_state import AnalysisStateStore, AnalysisStateStoreError
from ioscoverage.storage.coverage_store import CoverageStore
from ioscoverage.storage.reports import CoverageReportStore
from ioscoverage.storage.tokens import TokenStore


def create_server(
    host: str,
    port: int,
    token: str,
    runtime_paths: RuntimePaths,
    project_registry: ProjectRegistry,
    max_file_bytes: int = DEFAULT_MAX_FILE_BYTES,
    logger: Optional[logging.Logger] = None,
    max_run_bytes: int = DEFAULT_MAX_RUN_BYTES,
    max_build_artifact_bytes: int = DEFAULT_MAX_BUILD_ARTIFACT_BYTES,
    developer_dir: Optional[Path] = None,
    analysis_orchestrator_factory: Optional[Callable[[], object]] = None,
    start_analysis_scheduler: bool = True,
    run_upload_token: Optional[str] = None,
    gitlab_mr_client: Optional[GitLabMRClient] = None,
) -> ProbeHTTPServer:
    """Creates a server whose mutable paths and projects are startup-fixed."""

    runtime_paths.prepare_directories()
    artifact_validator = XcodeArtifactValidator(developer_dir or DEFAULT_DEVELOPER_DIR)
    coverage_store = CoverageStore(
        runtime_paths.database, runtime_paths.data_dir, project_registry, artifact_validator
    )
    analysis_state_store = AnalysisStateStore(runtime_paths.database)
    report_store = CoverageReportStore(runtime_paths.analysis_dir, analysis_state_store)
    if analysis_orchestrator_factory is None:
        analysis_configuration = AnalysisConfiguration(
            database_path=runtime_paths.database,
            data_dir=runtime_paths.data_dir,
            analysis_root=runtime_paths.analysis_dir / "single-build",
            project_registry=project_registry,
            developer_dir=(developer_dir or DEFAULT_DEVELOPER_DIR).resolve(),
        )
        analysis_orchestrator_factory = lambda: CycleAnalysisOrchestrator(analysis_configuration)
    scheduler = AnalysisScheduler(analysis_state_store, analysis_orchestrator_factory) if start_analysis_scheduler else None
    config = ProbeConfig(
        token=token,
        data_dir=runtime_paths.data_dir,
        max_file_bytes=max_file_bytes,
        max_run_bytes=max_run_bytes,
        max_build_artifact_bytes=max_build_artifact_bytes,
        instance_id=str(uuid.uuid4()),
        logger=logger or logging.getLogger("ios_coverage_server.null"),
        coverage_store=coverage_store,
        analysis_state_store=analysis_state_store,
        analysis_scheduler=scheduler,
        run_upload_token=run_upload_token,
        gitlab_mr_client=gitlab_mr_client,
        report_store=report_store,
    )
    return ProbeHTTPServer((host, port), config)


def parse_arguments(arguments=None) -> argparse.Namespace:
    """Parses bounded operational limits; runtime root comes from server config."""

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="0.0.0.0", help="listen address")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help="listen port")
    parser.add_argument("--rotate-operator-token", action="store_true", help="rotate operator credential")
    parser.add_argument("--max-file-mib", type=int, default=5)
    parser.add_argument("--max-run-mib", type=int, default=100)
    parser.add_argument("--max-build-artifact-mib", type=int, default=512)
    return parser.parse_args(arguments)


def main() -> None:
    """Loads immutable deployment configuration and starts the LAN service."""

    arguments = parse_arguments()
    if not 1 <= arguments.port <= 65535:
        raise SystemExit("--port must be between 1 and 65535")
    if not 1 <= arguments.max_file_mib <= 100 or not 1 <= arguments.max_run_mib <= 4096 or not 1 <= arguments.max_build_artifact_mib <= 4096:
        raise SystemExit("upload size limits are invalid")
    try:
        paths = ServerDeploymentConfiguration.load().runtime_paths
        paths.prepare_directories()
        registry = ProjectRegistry.load(paths.projects_file)
    except (ServerDeploymentConfigurationError, RuntimeConfigurationError, ProjectRegistryError) as error:
        raise SystemExit(getattr(error, "message", str(error)))
    operator_store = TokenStore(paths.operator_token)
    run_store = TokenStore(paths.run_upload_token)
    try:
        operator_token = operator_store.rotate() if arguments.rotate_operator_token else operator_store.load_or_create()
        run_token = run_store.load_or_create()
    except ValueError as error:
        raise SystemExit(str(error))
    if secrets.compare_digest(operator_token, run_token):
        raise SystemExit("operator and Run upload tokens must be different")
    gitlab_client = GitLabMRClient(paths.gitlab_token, registry)
    logger = configure_logging(paths.server_log)
    try:
        server = create_server(
            arguments.host, arguments.port, operator_token, paths, registry,
            arguments.max_file_mib * 1024 * 1024, logger,
            arguments.max_run_mib * 1024 * 1024,
            arguments.max_build_artifact_mib * 1024 * 1024,
            run_upload_token=run_token, gitlab_mr_client=gitlab_client,
        )
    except (ArtifactValidationError, AnalysisStateStoreError, ProjectRegistryError) as error:
        raise SystemExit(getattr(error, "message", str(error)))
    write_log(logger, "server_started", host=arguments.host, port=server.server_port,
              runtime_root=str(paths.root), project_count=len(registry.projects),
              max_probe_bytes=server.config.max_file_bytes,
              max_run_bytes=server.config.max_run_bytes,
              max_build_artifact_bytes=server.config.max_build_artifact_bytes,
              instance_id=server.config.instance_id)
    print("iOS coverage platform server started", flush=True)
    print("Listen: http://{}:{}".format(arguments.host, server.server_port), flush=True)
    print("Runtime root: {}".format(paths.root), flush=True)
    print("Operator token file: {}".format(paths.operator_token), flush=True)
    print("Run upload token file: {}".format(paths.run_upload_token), flush=True)
    print("Projects: {}".format(", ".join(item.project for item in registry.projects)), flush=True)
    print("Stop: press Control-C", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping...", flush=True)
    finally:
        write_log(logger, "server_stopped", instance_id=server.config.instance_id)
        server.server_close()


if __name__ == "__main__":
    main()
