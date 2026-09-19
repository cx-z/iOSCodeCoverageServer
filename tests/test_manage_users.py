#!/usr/bin/env python3
"""CLI tests for safe first-administrator creation."""

import io
import json
import sqlite3
import subprocess
import tempfile
import unittest
from pathlib import Path

from ioscoverage.cli.manage_users import run
from ioscoverage.integrations.xcode_artifacts import XcodeArtifactValidator
from ioscoverage.storage.coverage_store import CoverageStore
from tests.helpers import project_registry


class ManageUsersTests(unittest.TestCase):
    """Verifies the recovery CLI without exposing password material."""

    def setUp(self) -> None:
        """Creates one migrated runtime and source-style deployment pointer."""

        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)
        repository = self.root / "ExampleApp"
        subprocess.run(
            ["git", "init", str(repository)],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        self.runtime = self.root / "runtime"
        CoverageStore(
            self.runtime / "instance" / "coverage.sqlite3",
            self.runtime / "data",
            project_registry(repository),
            XcodeArtifactValidator(Path("/Applications/Xcode263.app/Contents/Developer")),
        )
        self.config = self.root / "server.json"
        self.config.write_text(
            json.dumps({"schema_version": 1, "runtime_root": str(self.runtime)}),
            encoding="utf-8",
        )

    def tearDown(self) -> None:
        """Removes the isolated runtime and configuration."""

        self.temporary_directory.cleanup()

    def invoke(self, values):
        """Runs create-admin with deterministic no-echo prompt values."""

        prompts = iter(values)
        stdout = io.StringIO()
        stderr = io.StringIO()
        status = run(
            ["create-admin", "--username", "First.Admin"],
            config_path=self.config,
            password_reader=lambda _prompt: next(prompts),
            stdout=stdout,
            stderr=stderr,
        )
        return status, stdout.getvalue(), stderr.getvalue()

    def test_create_admin_success_and_secret_free_output(self) -> None:
        """Creates an active admin while keeping password and digest out of output."""

        secret = "owner-password-value"
        status, stdout, stderr = self.invoke([secret, secret])
        self.assertEqual(status, 0)
        self.assertEqual(stderr, "")
        self.assertIn("First.Admin", stdout)
        self.assertNotIn(secret, stdout)
        with sqlite3.connect(str(self.runtime / "instance" / "coverage.sqlite3")) as connection:
            role, state, digest = connection.execute(
                "SELECT role, status, password_digest FROM platform_users"
            ).fetchone()
        self.assertEqual((role, state), ("admin", "active"))
        self.assertNotEqual(bytes(digest), secret.encode())

    def test_confirmation_mismatch_and_conflict_are_safe(self) -> None:
        """Rejects mismatched prompts and a repeated username without printing secrets."""

        secret = "owner-password-value"
        status, stdout, stderr = self.invoke([secret, "different-password"])
        self.assertEqual(status, 2)
        self.assertEqual(stdout, "")
        self.assertNotIn(secret, stderr)
        self.assertEqual(self.invoke([secret, secret])[0], 0)
        status, stdout, stderr = self.invoke(["replacement-password", "replacement-password"])
        self.assertEqual(status, 1)
        self.assertEqual(stdout, "")
        self.assertIn("already exists", stderr)
        self.assertNotIn("replacement-password", stderr)

    def test_invalid_configuration_returns_bounded_error(self) -> None:
        """Rejects a missing runtime pointer without creating account state."""

        missing = self.root / "missing.json"
        stderr = io.StringIO()
        status = run(
            ["create-admin", "--username", "First.Admin"],
            config_path=missing,
            password_reader=lambda _prompt: "unused-password",
            stdout=io.StringIO(),
            stderr=stderr,
        )
        self.assertEqual(status, 1)
        self.assertIn("configuration is missing", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
