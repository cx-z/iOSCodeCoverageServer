#!/usr/bin/env python3
"""CLI entry point for one immutable Cycle cumulative-coverage snapshot."""

import argparse
import json
import sys
from pathlib import Path
from typing import Dict, List, Optional

from ioscoverage.analysis.cumulative.analyzer import CycleCoverageAnalysisError, CycleCoverageAnalyzer
from ioscoverage.analysis.single_build.analyzer import AnalysisConfiguration, SingleBuildAnalysisError
from ioscoverage.lifecycle.runtime_lock import RuntimeLockError, RuntimeProcessLock
from ioscoverage.runtime import RuntimePaths


def parse_arguments(arguments: Optional[List[str]] = None) -> argparse.Namespace:
    """Accepts only Cycle, current Build, and immutable Build/input identities."""

    parser = argparse.ArgumentParser(
        description=(
            "Accumulate compiler-region hits across completed iOS Builds without merging profiles."
        )
    )
    parser.add_argument("--runtime-root", type=Path, required=True)
    parser.add_argument("--cycle-id", required=True, help="registered Cycle UUID")
    parser.add_argument(
        "--current-build-id", required=True, help="registered current Build UUID"
    )
    parser.add_argument(
        "--build-input",
        action="append",
        required=True,
        metavar="BUILD_ID=INPUT_SET_SHA256",
        help="completed single-Build identity; repeat once per selected Build",
    )
    return parser.parse_args(arguments)


def parse_build_inputs(values: List[str]) -> Dict[str, str]:
    """Parses repeated identities while preserving duplicate rejection."""

    result: Dict[str, str] = {}
    for value in values:
        if value.count("=") != 1:
            raise CycleCoverageAnalysisError(
                "build_input_invalid",
                "--build-input must be BUILD_ID=INPUT_SET_SHA256",
                2,
            )
        build_id, input_set_sha256 = value.split("=", 1)
        if not build_id or not input_set_sha256:
            raise CycleCoverageAnalysisError(
                "build_input_invalid",
                "--build-input must contain both identities",
                2,
            )
        if build_id in result:
            raise CycleCoverageAnalysisError(
                "duplicate_build_input", "Build input is duplicated", 2
            )
        result[build_id] = input_set_sha256
    return result


def main(arguments: Optional[List[str]] = None) -> int:
    """Runs the read-only cumulative analyzer and emits one JSON response."""

    parsed = parse_arguments(arguments)
    try:
        build_inputs = parse_build_inputs(parsed.build_input)
        configuration = AnalysisConfiguration.for_runtime_root(parsed.runtime_root)
        with RuntimeProcessLock(RuntimePaths.from_root(parsed.runtime_root).server_lock):
            result = CycleCoverageAnalyzer(configuration).analyze(
                parsed.cycle_id, parsed.current_build_id, build_inputs
            )
    except (CycleCoverageAnalysisError, SingleBuildAnalysisError) as error:
        print(
            json.dumps(
                {"status": "failed", "error": error.code, "message": error.message},
                ensure_ascii=False,
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return error.exit_code
    except RuntimeLockError as error:
        print(
            json.dumps(
                {"status": "failed", "error": "runtime_busy", "message": str(error)},
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
                "cycle_input_set_sha256": result.cycle_input_set_sha256,
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
