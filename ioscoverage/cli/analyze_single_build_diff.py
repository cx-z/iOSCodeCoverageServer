#!/usr/bin/env python3
"""CLI entry point for one fixed single-Build changed-line analysis."""

import argparse
import json
import sys
from pathlib import Path
from typing import List, Optional

from ioscoverage.analysis.single_build.analyzer import AnalysisConfiguration, SingleBuildAnalysisError
from ioscoverage.analysis.diff.analyzer import (
    SingleBuildDiffAnalysisError,
    SingleBuildDiffAnalyzer,
)


def parse_arguments(arguments: Optional[List[str]] = None) -> argparse.Namespace:
    """Accepts only the registered Cycle, Build, and prior input-set identities."""

    parser = argparse.ArgumentParser(
        description="Intersect one completed iOS single-Build coverage export with its fixed Git diff."
    )
    parser.add_argument("--runtime-root", type=Path, required=True)
    parser.add_argument("--cycle-id", required=True, help="registered Cycle UUID")
    parser.add_argument("--build-id", required=True, help="registered Build UUID")
    parser.add_argument(
        "--input-set-sha256", required=True, help="completed single-Build input set SHA-256"
    )
    return parser.parse_args(arguments)


def main(arguments: Optional[List[str]] = None) -> int:
    """Runs the read-only analyzer and emits one machine-readable result."""

    parsed = parse_arguments(arguments)
    try:
        configuration = AnalysisConfiguration.for_runtime_root(parsed.runtime_root)
        result = SingleBuildDiffAnalyzer(configuration).analyze(
            parsed.cycle_id, parsed.build_id, parsed.input_set_sha256
        )
    except (SingleBuildDiffAnalysisError, SingleBuildAnalysisError) as error:
        print(
            json.dumps(
                {"status": "failed", "error": error.code, "message": error.message},
                ensure_ascii=False,
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return error.exit_code
    print(
        json.dumps(
            {
                "status": "complete",
                "diff_set_sha256": result.diff_set_sha256,
                "output_directory": str(result.output_directory),
                "idempotent_replay": result.idempotent_replay,
                "summary": result.summary,
            },
            ensure_ascii=False,
            sort_keys=True,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
