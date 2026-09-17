"""Tests for the zero-argument server runtime configuration boundary."""

import json
import tempfile
import unittest
from pathlib import Path

from ioscoverage.server.deployment import (
    ServerDeploymentConfiguration,
    ServerDeploymentConfigurationError,
)


class ServerDeploymentConfigurationTests(unittest.TestCase):
    """Keeps one source-side pointer separate from all mutable runtime data."""

    def setUp(self) -> None:
        """Creates one isolated configuration and candidate runtime root."""

        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.config = self.root / "server.json"
        self.runtime = self.root / "runtime"

    def tearDown(self) -> None:
        """Removes only this test's temporary files."""

        self.temporary.cleanup()

    def write(self, payload: object) -> None:
        """Writes one JSON fixture without creating runtime directories."""

        self.config.write_text(json.dumps(payload), encoding="utf-8")

    def test_valid_absolute_runtime_root_is_normalized(self) -> None:
        """Resolves the configured root without opening or creating runtime files."""

        self.write({"schema_version": 1, "runtime_root": str(self.runtime / ".." / "runtime")})
        deployment = ServerDeploymentConfiguration.load(self.config)
        self.assertEqual(deployment.runtime_paths.root, self.runtime.resolve())
        self.assertFalse(self.runtime.exists())

    def test_missing_or_invalid_json_reports_configuration_error(self) -> None:
        """Stops before startup for absent or unreadable deployment configuration."""

        with self.assertRaises(ServerDeploymentConfigurationError):
            ServerDeploymentConfiguration.load(self.config)
        self.config.write_text("not-json", encoding="utf-8")
        with self.assertRaises(ServerDeploymentConfigurationError):
            ServerDeploymentConfiguration.load(self.config)

    def test_empty_runtime_root_is_rejected_without_side_effects(self) -> None:
        """Requires the developer to configure a path before service startup."""

        self.write({"schema_version": 1, "runtime_root": ""})
        with self.assertRaisesRegex(ServerDeploymentConfigurationError, "runtime_root is empty"):
            ServerDeploymentConfiguration.load(self.config)
        self.assertFalse(self.runtime.exists())

    def test_unknown_missing_version_and_relative_values_are_rejected(self) -> None:
        """Keeps the source-side configuration intentionally small and unambiguous."""

        cases = (
            {"schema_version": 1, "runtime_root": str(self.runtime), "extra": True},
            {"schema_version": 1},
            {"schema_version": 2, "runtime_root": str(self.runtime)},
            {"schema_version": 1, "runtime_root": "relative/runtime"},
            {"schema_version": 1, "runtime_root": "/"},
        )
        for payload in cases:
            with self.subTest(payload=payload):
                self.write(payload)
                with self.assertRaises(ServerDeploymentConfigurationError):
                    ServerDeploymentConfiguration.load(self.config)

    def test_anonymous_read_is_optional_boolean_defaulting_false(self) -> None:
        """Accepts only booleans for the read-only flag and defaults to token-protected."""

        self.write({"schema_version": 1, "runtime_root": str(self.runtime)})
        self.assertFalse(ServerDeploymentConfiguration.load(self.config).anonymous_read)
        for value in (True, False):
            with self.subTest(value=value):
                self.write({
                    "schema_version": 1,
                    "runtime_root": str(self.runtime),
                    "anonymous_read": value,
                })
                self.assertEqual(
                    ServerDeploymentConfiguration.load(self.config).anonymous_read, value
                )

    def test_anonymous_read_non_boolean_is_rejected(self) -> None:
        """Rejects truthy or stringy flag values before any runtime file is opened."""

        for value in ("yes", 1, None, []):
            with self.subTest(value=value):
                self.write({
                    "schema_version": 1,
                    "runtime_root": str(self.runtime),
                    "anonymous_read": value,
                })
                with self.assertRaisesRegex(
                    ServerDeploymentConfigurationError, "anonymous_read"
                ):
                    ServerDeploymentConfiguration.load(self.config)


if __name__ == "__main__":
    unittest.main()
