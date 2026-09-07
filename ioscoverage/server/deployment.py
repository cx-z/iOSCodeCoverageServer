"""Source-owned pointer to the external mutable runtime root."""

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

from ioscoverage.runtime import RuntimeConfigurationError, RuntimePaths


DEFAULT_SERVER_CONFIG_PATH = Path(__file__).resolve().parents[2] / "config" / "server.json"


class ServerDeploymentConfigurationError(ValueError):
    """Reports a safe startup configuration error before runtime files are opened."""


@dataclass(frozen=True)
class ServerDeploymentConfiguration:
    """Contains the single external runtime root selected by the developer."""

    runtime_paths: RuntimePaths

    @classmethod
    def load(cls, path: Path = DEFAULT_SERVER_CONFIG_PATH) -> "ServerDeploymentConfiguration":
        """Loads strict schema version 1 JSON and validates its runtime root."""

        config_path = Path(path).expanduser().resolve()
        try:
            payload = json.loads(config_path.read_text(encoding="utf-8"))
        except FileNotFoundError as error:
            raise ServerDeploymentConfigurationError(
                "server runtime configuration is missing; copy config/server.example.json "
                "to config/server.json and set runtime_root"
            ) from error
        except (OSError, UnicodeError, ValueError) as error:
            raise ServerDeploymentConfigurationError(
                "config/server.json must be valid UTF-8 JSON"
            ) from error
        return cls(runtime_paths=cls._parse_runtime_paths(payload))

    @staticmethod
    def _parse_runtime_paths(payload: object) -> RuntimePaths:
        """Accepts only schema_version and one non-empty absolute runtime_root."""

        if not isinstance(payload, Mapping) or set(payload) != {"schema_version", "runtime_root"}:
            raise ServerDeploymentConfigurationError(
                "config/server.json has unknown or missing fields"
            )
        if payload["schema_version"] != 1:
            raise ServerDeploymentConfigurationError(
                "config/server.json schema_version must be 1"
            )
        runtime_root = payload["runtime_root"]
        if not isinstance(runtime_root, str) or not runtime_root.strip():
            raise ServerDeploymentConfigurationError(
                "runtime_root is empty in config/server.json; set an absolute data directory"
            )
        candidate = Path(runtime_root).expanduser()
        if not candidate.is_absolute():
            raise ServerDeploymentConfigurationError(
                "runtime_root in config/server.json must be an absolute path"
            )
        try:
            return RuntimePaths.from_root(candidate)
        except RuntimeConfigurationError as error:
            raise ServerDeploymentConfigurationError(str(error)) from error
