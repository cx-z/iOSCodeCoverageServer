#!/usr/bin/env python3
"""Private child-process entry for one process-isolated Cycle analysis attempt."""

import argparse
import json
import sys
from pathlib import Path
from typing import Mapping

from ioscoverage.analysis.orchestrator import (
    CycleAnalysisOrchestrationError,
    CycleAnalysisOrchestrator,
)
from ioscoverage.analysis.single_build.analyzer import AnalysisConfiguration
from ioscoverage.lifecycle.runtime_lock import (
    RuntimeLockError,
    validate_inherited_runtime_lock,
)
from ioscoverage.projects import ProjectRegistry, ProjectRegistryError
from ioscoverage.runtime import RuntimeConfigurationError, RuntimePaths


MAX_REGISTRY_SNAPSHOT_BYTES = 1024 * 1024


def parse_arguments(arguments=None) -> argparse.Namespace:
    """Parses the fixed internal identities supplied by the owning server process."""

    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--runtime-root", required=True)
    parser.add_argument("--developer-dir", required=True)
    parser.add_argument("--cycle-id", required=True)
    return parser.parse_args(arguments)


def execute(arguments: argparse.Namespace) -> Mapping[str, object]:
    """Loads the startup-pinned registry and delegates to the existing orchestrator."""

    paths = RuntimePaths.from_root(Path(arguments.runtime_root))
    validate_inherited_runtime_lock(paths.server_lock)
    registry_bytes = sys.stdin.buffer.read(MAX_REGISTRY_SNAPSHOT_BYTES + 1)
    if not registry_bytes or len(registry_bytes) > MAX_REGISTRY_SNAPSHOT_BYTES:
        raise CycleAnalysisOrchestrationError(
            "snapshot",
            "project_registry_invalid",
            "project registry snapshot is invalid",
        )
    try:
        registry_document = json.loads(registry_bytes.decode("utf-8"))
    except (UnicodeError, ValueError) as error:
        raise CycleAnalysisOrchestrationError(
            "snapshot",
            "project_registry_invalid",
            "project registry snapshot is invalid",
        ) from error
    registry = ProjectRegistry.from_document(registry_document)
    configuration = AnalysisConfiguration(
        database_path=paths.database,
        data_dir=paths.data_dir,
        analysis_root=paths.analysis_dir / "single-build",
        project_registry=registry,
        developer_dir=Path(arguments.developer_dir).resolve(),
    )
    result = CycleAnalysisOrchestrator(configuration).analyze(arguments.cycle_id)
    return {
        "status": "complete",
        "result": {
            "orchestration_input_sha256": result.orchestration_input_sha256,
            "output_directory": str(result.output_directory),
            "summary": result.summary,
            "idempotent_replay": result.idempotent_replay,
        },
    }


def main(arguments=None) -> int:
    """Writes one JSON response while retaining the inherited runtime-lock lease."""

    try:
        payload = execute(parse_arguments(arguments))
        exit_code = 0
    except CycleAnalysisOrchestrationError as error:
        payload = {
            "status": "failed",
            "stage": error.stage,
            "code": error.code,
            "message": error.message,
        }
        exit_code = error.exit_code
    except (ProjectRegistryError, RuntimeConfigurationError, RuntimeLockError) as error:
        payload = {
            "status": "failed",
            "stage": "snapshot",
            "code": getattr(error, "code", "analysis_configuration_invalid"),
            "message": getattr(error, "message", "analysis configuration is invalid"),
        }
        exit_code = 3
    except Exception:
        payload = {
            "status": "failed",
            "stage": "orchestration",
            "code": "analysis_internal_error",
            "message": "background Cycle analysis failed unexpectedly",
        }
        exit_code = 3
    print(json.dumps(payload, sort_keys=True, separators=(",", ":")), flush=True)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
