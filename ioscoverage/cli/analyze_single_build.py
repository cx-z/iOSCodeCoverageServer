#!/usr/bin/env python3
"""CLI entry point for one restricted local iOS Build analysis."""

import argparse
import json
import sys
from pathlib import Path
from typing import List, Optional

from ioscoverage.integrations.git_repository import GitRepositoryError
from ioscoverage.analysis.single_build.analyzer import (
    AnalysisConfiguration,
    SingleBuildAnalysisError,
    SingleBuildAnalyzer,
)


def parse_arguments(arguments: Optional[List[str]] = None) -> argparse.Namespace:
    """Accepts only Cycle, Build, and explicitly repeated Run identities."""

    parser = argparse.ArgumentParser(
        description="Analyze explicit Runs from one registered iOS Build using Xcode 26.3."
    )
    parser.add_argument("--runtime-root", type=Path, required=True)
    parser.add_argument("--cycle-id", required=True, help="registered Cycle UUID")
    parser.add_argument("--build-id", required=True, help="registered Build UUID")
    parser.add_argument(
        "--run-id",
        required=True,
        action="append",
        dest="run_ids",
        help="registered Run UUID; repeat for every input Run",
    )
    return parser.parse_args(arguments)


def main(arguments: Optional[List[str]] = None) -> int:
    """Runs the analyzer and emits one machine-readable success or error object."""

    parsed = parse_arguments(arguments)
    try:
        configuration = AnalysisConfiguration.for_runtime_root(parsed.runtime_root)
        analyzer = SingleBuildAnalyzer(configuration)
        result = analyzer.analyze(parsed.cycle_id, parsed.build_id, parsed.run_ids)
    except SingleBuildAnalysisError as error:
        print(
            json.dumps(
                {"status": "failed", "error": error.code, "message": error.message},
                ensure_ascii=False,
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return error.exit_code
    except GitRepositoryError as error:
        print(
            json.dumps(
                {"status": "failed", "error": error.code, "message": error.message},
                ensure_ascii=False,
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 3
    print(
        json.dumps(
            {
                "status": "complete",
                "input_set_sha256": result.input_set_sha256,
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
