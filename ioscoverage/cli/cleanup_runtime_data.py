"""CLI for dependency-driven runtime cleanup; dry-run is the default."""

import argparse
import json
from typing import Optional, Sequence

from ioscoverage.lifecycle.cleanup import RuntimeCleanupError, RuntimeDataCleaner
from ioscoverage.server.deployment import (
    ServerDeploymentConfiguration,
    ServerDeploymentConfigurationError,
)


def build_parser() -> argparse.ArgumentParser:
    """Exposes only the explicit destructive switch; runtime_root stays configured."""

    parser = argparse.ArgumentParser(
        description="Safely compact configured iOS Coverage Server runtime data (dry-run by default)."
    )
    parser.add_argument(
        "--apply", action="store_true",
        help="apply the verified deletion plan; the coverage server must be stopped",
    )
    return parser


def main(arguments: Optional[Sequence[str]] = None) -> int:
    """Loads the server runtime configuration and emits one JSON summary."""

    parsed = build_parser().parse_args(arguments)
    try:
        paths = ServerDeploymentConfiguration.load().runtime_paths
        result = RuntimeDataCleaner(paths).run(apply=parsed.apply)
    except (ServerDeploymentConfigurationError, RuntimeCleanupError) as error:
        print(json.dumps({
            "status": "failed", "error": getattr(error, "code", "configuration_error"),
            "message": getattr(error, "message", str(error)), "dry_run": not parsed.apply,
        }, sort_keys=True))
        return 3
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
