"""Immutable HTTP service configuration and protocol constants."""

import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from ioscoverage.analysis.scheduler import AnalysisScheduler
from ioscoverage.lifecycle.manager import RuntimeCleanupManager
from ioscoverage.integrations.gitlab_mr import GitLabMRClient
from ioscoverage.server.mr_coverage_resolver import MergeRequestCoverageResolver
from ioscoverage.storage.analysis_state import AnalysisStateStore
from ioscoverage.storage.coverage_store import CoverageStore
from ioscoverage.storage.reports import CoverageReportStore


SERVER_VERSION = "ios-coverage-server/1.0"
DEFAULT_PORT = 8765
DEFAULT_MAX_FILE_BYTES = 5 * 1024 * 1024
DEFAULT_MAX_RUN_BYTES = 100 * 1024 * 1024
DEFAULT_MAX_BUILD_ARTIFACT_BYTES = 512 * 1024 * 1024
MULTIPART_OVERHEAD_BYTES = 1024 * 1024
MAX_JSON_BYTES = 64 * 1024
SHA256_PATTERN = re.compile(r"^[0-9a-fA-F]{64}$")


@dataclass(frozen=True)
class ProbeConfig:
    """Owns immutable runtime configuration shared by request threads."""

    token: str
    data_dir: Path
    max_file_bytes: int
    max_run_bytes: int
    max_build_artifact_bytes: int
    instance_id: str
    logger: logging.Logger
    coverage_store: CoverageStore
    analysis_state_store: AnalysisStateStore
    analysis_scheduler: Optional[AnalysisScheduler]
    runtime_cleanup_manager: Optional[RuntimeCleanupManager] = None
    run_upload_token: Optional[str] = None
    gitlab_mr_client: Optional[GitLabMRClient] = None
    report_store: Optional[CoverageReportStore] = None
    merge_request_coverage_resolver: Optional[MergeRequestCoverageResolver] = None
