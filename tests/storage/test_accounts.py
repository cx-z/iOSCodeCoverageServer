#!/usr/bin/env python3
"""Focused AccountStore transaction and session lifecycle tests."""

import hashlib
import sqlite3
import subprocess
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from ioscoverage.integrations.xcode_artifacts import XcodeArtifactValidator
from ioscoverage.storage.accounts import AccountStore, AccountStoreError
from ioscoverage.storage.coverage_store import CoverageStore
from tests.helpers import project_registry


class MutableClock:
    """Provides a deterministic timezone-aware clock for session expiry checks."""

    def __init__(self) -> None:
        """Starts at a fixed neutral UTC instant."""

        self.value = datetime(2026, 1, 1, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        """Returns the current injected instant."""

        return self.value


class AccountStoreTests(unittest.TestCase):
    """Verifies user uniqueness, atomic invalidation, and hashed sessions."""

    def setUp(self) -> None:
        """Creates an isolated v4 database and account store."""

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
        self.database = self.root / "instance" / "coverage.sqlite3"
        CoverageStore(
            self.database,
            self.root / "data",
            project_registry(repository),
            XcodeArtifactValidator(Path("/Applications/Xcode263.app/Contents/Developer")),
        )
        self.clock = MutableClock()
        self.next_token = "session-token-value-that-is-longer-than-thirty-two-bytes"
        self.store = AccountStore(
            self.database, clock=self.clock, token_factory=lambda: self.next_token
        )

    def tearDown(self) -> None:
        """Removes all temporary account data."""

        self.temporary_directory.cleanup()

    def test_case_insensitive_username_conflict_and_safe_projection(self) -> None:
        """Keeps one username identity and never projects password storage fields."""

        user = self.store.create_user("Developer.One", "password-one", "developer")
        with self.assertRaises(AccountStoreError) as caught:
            self.store.create_user("developer.one", "password-two", "developer")
        self.assertEqual(caught.exception.code, "username_conflict")
        self.assertEqual(set(user), {"id", "username", "role", "status", "created_at", "updated_at"})

    def test_concurrent_registration_has_one_winner(self) -> None:
        """Relies on the SQLite unique key so concurrent case variants cannot both succeed."""

        barrier = threading.Barrier(2)
        results = []

        def create(value: str) -> None:
            """Attempts one registration after both workers reach the same boundary."""

            barrier.wait()
            try:
                self.store.create_user(value, "password-value", "developer")
                results.append("created")
            except AccountStoreError as error:
                results.append(error.code)

        threads = [
            threading.Thread(target=create, args=("Concurrent.User",)),
            threading.Thread(target=create, args=("concurrent.user",)),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=5)
        self.assertCountEqual(results, ["created", "username_conflict"])

    def test_session_stores_only_digest_and_expires_without_sliding(self) -> None:
        """Persists only SHA-256 and removes a session after its fixed seven-day lifetime."""

        user = self.store.create_user("session-user", "password-value", "developer")
        session = self.store.create_session(user["id"])
        self.assertEqual(session["token"], self.next_token)
        with sqlite3.connect(str(self.database)) as connection:
            row = connection.execute(
                "SELECT token_sha256, expires_at FROM web_sessions"
            ).fetchone()
            database_bytes = self.database.read_bytes()
        self.assertEqual(row[0], hashlib.sha256(self.next_token.encode()).hexdigest())
        self.assertNotIn(self.next_token.encode(), database_bytes)
        self.assertEqual(self.store.resolve_session(self.next_token)["id"], user["id"])
        self.clock.value += timedelta(days=7, seconds=1)
        self.assertIsNone(self.store.resolve_session(self.next_token))

    def test_disable_and_password_reset_invalidate_all_sessions(self) -> None:
        """Performs account mutation and multi-session invalidation in one transaction."""

        user = self.store.create_user("multi-user", "password-value", "developer")
        first = self.store.create_session(user["id"])["token"]
        self.next_token = "second-session-token-value-that-is-also-long-enough"
        second = self.store.create_session(user["id"])["token"]
        self.store.reset_password(user["id"], "replacement-password")
        self.assertIsNone(self.store.resolve_session(first))
        self.assertIsNone(self.store.resolve_session(second))
        self.assertIsNone(self.store.authenticate("multi-user", "password-value"))
        self.assertEqual(
            self.store.authenticate("multi-user", "replacement-password")["id"], user["id"]
        )
        third = self.store.create_session(user["id"])["token"]
        self.store.set_status(user["id"], "disabled")
        self.assertIsNone(self.store.resolve_session(third))
        self.assertIsNone(self.store.authenticate("multi-user", "replacement-password"))

    def test_logout_removes_only_current_session(self) -> None:
        """Allows parallel browser sessions while invalidating only the requested one."""

        user = self.store.create_user("logout-user", "password-value", "developer")
        first = self.store.create_session(user["id"])["token"]
        self.next_token = "parallel-session-token-value-that-is-long-enough"
        second = self.store.create_session(user["id"])["token"]
        self.store.logout(first)
        self.assertIsNone(self.store.resolve_session(first))
        self.assertEqual(self.store.resolve_session(second)["id"], user["id"])


if __name__ == "__main__":
    unittest.main()
