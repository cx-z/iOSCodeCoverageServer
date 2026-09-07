#!/usr/bin/env python3
"""CLI entry point for one-click analysis of the latest ready Build in a Cycle."""

import argparse
import json
from pathlib import Path
from typing import Optional, Sequence

from ioscoverage.analysis.orchestrator import (
    CycleAnalysisOrchestrationError,
    CycleAnalysisOrchestrator,
)
from ioscoverage.analysis.single_build.analyzer import AnalysisConfiguration, SingleBuildAnalysisError


def build_parser() -> argparse.ArgumentParser:
    """Defines the intentionally narrow identity-only CLI surface."""

    parser = argparse.ArgumentParser(
        description="Analyze all eligible immutable Build/Run inputs for one registered Cycle."
    )
    parser.add_argument("--runtime-root", type=Path, required=True)
    parser.add_argument("--cycle-id", required=True, help="registered immutable Cycle UUID")
    return parser


def main(arguments: Optional[Sequence[str]] = None) -> int:
    """Runs the orchestration and emits one machine-readable result object."""

    parsed = build_parser().parse_args(arguments)
    try:
        configuration = AnalysisConfiguration.for_runtime_root(parsed.runtime_root)
        result = CycleAnalysisOrchestrator(configuration).analyze(parsed.cycle_id)
    except (CycleAnalysisOrchestrationError, SingleBuildAnalysisError) as error:
        stage = getattr(error, "stage", "configuration")
        print(
            json.dumps(
                {
                    "status": "failed",
                    "failure_stage": stage,
                    "error": error.code,
                    "message": error.message,
                },
                sort_keys=True,
            )
        )
        return error.exit_code
    print(
        json.dumps(
            {
                "status": "complete",
                "cycle_id": parsed.cycle_id,
                "orchestration_input_sha256": result.orchestration_input_sha256,
                "output_directory": str(result.output_directory),
                "current_build_id": result.summary["current_build_id"],
                "selected_builds": result.summary["build_inputs"],
                "selected_run_count": result.summary["selected_run_count"],
                "skipped_build_count": result.summary["skipped_build_count"],
                "cycle_input_set_sha256": result.summary["cycle_input_set_sha256"],
                "idempotent_replay": result.idempotent_replay,
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
