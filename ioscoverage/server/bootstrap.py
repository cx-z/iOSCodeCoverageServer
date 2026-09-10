#!/usr/bin/env python3
"""Service dependency assembly for the multi-project coverage platform."""

import argparse
import logging
import secrets
import signal
import uuid
from pathlib import Path
from typing import Callable, Optional

from ioscoverage.analysis.process_orchestrator import ProcessCycleAnalysisOrchestrator
from ioscoverage.analysis.scheduler import AnalysisScheduler
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
from ioscoverage.server.mr_coverage_resolver import MergeRequestCoverageResolver
from ioscoverage.server.logging import configure_logging, write_log
from ioscoverage.storage.analysis_state import AnalysisStateStore, AnalysisStateStoreError
from ioscoverage.storage.coverage_store import CoverageStore
from ioscoverage.storage.reports import CoverageReportStore
from ioscoverage.storage.tokens import TokenStore
from ioscoverage.storage.retained_evidence import RetainedBuildEvidenceStore
from ioscoverage.lifecycle.runtime_lock import RuntimeLockError, RuntimeProcessLock
from ioscoverage.lifecycle.cleanup import RuntimeDataCleaner
from ioscoverage.lifecycle.manager import (
    DEFAULT_CLEANUP_INTERVAL_SECONDS,
    RuntimeCleanupManager,
)


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
    runtime_process_lock: Optional[RuntimeProcessLock] = None,
    enable_runtime_cleanup: bool = False,
    cleanup_interval_seconds: float = DEFAULT_CLEANUP_INTERVAL_SECONDS,
) -> ProbeHTTPServer:
    """Creates a server whose mutable paths and projects are startup-fixed."""

    runtime_paths.prepare_directories()
    artifact_validator = XcodeArtifactValidator(developer_dir or DEFAULT_DEVELOPER_DIR)
    retained_evidence_store = RetainedBuildEvidenceStore(runtime_paths.analysis_dir)
    coverage_store = CoverageStore(
        runtime_paths.database, runtime_paths.data_dir, project_registry, artifact_validator,
        retained_evidence_store=retained_evidence_store,
    )
    analysis_state_store = AnalysisStateStore(runtime_paths.database)
    report_store = CoverageReportStore(runtime_paths.analysis_dir, analysis_state_store)
    cancel_active_analysis = None
    active_analysis_check = None
    if start_analysis_scheduler and analysis_orchestrator_factory is None:
        process_orchestrator = ProcessCycleAnalysisOrchestrator(
            runtime_paths.root,
            project_registry.to_document(),
            developer_dir or DEFAULT_DEVELOPER_DIR,
            runtime_lock_descriptor=(
                runtime_process_lock.lease_descriptor
                if runtime_process_lock is not None
                else None
            ),
        )
        analysis_orchestrator_factory = lambda: process_orchestrator
        cancel_active_analysis = process_orchestrator.cancel_active
        active_analysis_check = process_orchestrator.has_active_process_group
    scheduler = (
        AnalysisScheduler(
            analysis_state_store,
            analysis_orchestrator_factory,
            cancel_active_analysis=cancel_active_analysis,
            active_analysis_check=active_analysis_check,
        )
        if start_analysis_scheduler
        else None
    )
    cleanup_manager = None
    if enable_runtime_cleanup:
        if scheduler is None or runtime_process_lock is None:
            raise ValueError("automatic cleanup requires the analysis scheduler and runtime lock")
        cleanup_manager = RuntimeCleanupManager(
            RuntimeDataCleaner(runtime_paths),
            runtime_process_lock,
            scheduler,
            coverage_store,
            logger or logging.getLogger("ios_coverage_server.null"),
            interval_seconds=cleanup_interval_seconds,
        )
    merge_request_coverage_resolver = (
        MergeRequestCoverageResolver(
            gitlab_mr_client, coverage_store, analysis_state_store, report_store
        )
        if gitlab_mr_client is not None
        else None
    )
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
        runtime_cleanup_manager=cleanup_manager,
        run_upload_token=run_upload_token,
        gitlab_mr_client=gitlab_mr_client,
        report_store=report_store,
        merge_request_coverage_resolver=merge_request_coverage_resolver,
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


def _handle_termination_signal(_signal_number, _frame) -> None:
    """Converts SIGTERM into the same coordinated shutdown used by Control-C."""

    raise KeyboardInterrupt


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
    runtime_lock = RuntimeProcessLock(paths.server_lock)
    try:
        runtime_lock.acquire()
        server = create_server(
            arguments.host, arguments.port, operator_token, paths, registry,
            arguments.max_file_mib * 1024 * 1024, logger,
            arguments.max_run_mib * 1024 * 1024,
            arguments.max_build_artifact_mib * 1024 * 1024,
            run_upload_token=run_token, gitlab_mr_client=gitlab_client,
            runtime_process_lock=runtime_lock, enable_runtime_cleanup=True,
        )
    except (ArtifactValidationError, AnalysisStateStoreError, ProjectRegistryError, RuntimeLockError) as error:
        runtime_lock.release()
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
    previous_sigterm_handler = signal.signal(signal.SIGTERM, _handle_termination_signal)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        print("\nStopping...", flush=True)
    finally:
        try:
            write_log(logger, "server_stopped", instance_id=server.config.instance_id)
            server.server_close()
            runtime_lock.release()
        finally:
            signal.signal(signal.SIGTERM, previous_sigterm_handler)


if __name__ == "__main__":
    main()
