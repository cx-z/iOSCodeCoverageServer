#!/usr/bin/env python3
"""Focused storage tests for atomic user-to-Cycle submission relationships."""

import sqlite3
import subprocess
import tempfile
import unittest
from pathlib import Path

from ioscoverage.integrations.xcode_artifacts import XcodeArtifactValidator
from ioscoverage.storage.accounts import AccountStore
from ioscoverage.storage.coverage_store import CoverageStore
from tests.helpers import project_registry


class CycleSubmitterStoreTests(unittest.TestCase):
    """Locks Cycle uniqueness, relationship idempotency, and rollback behavior."""

    def setUp(self) -> None:
        """Creates one isolated v4 store and two active developers."""

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
        self.store = CoverageStore(
            self.database,
            self.root / "data",
            project_registry(repository),
            XcodeArtifactValidator(Path("/Applications/Xcode263.app/Contents/Developer")),
        )
        accounts = AccountStore(self.database)
        self.user_a = accounts.create_user("developer-a", "password-value", "developer")
        self.user_b = accounts.create_user("developer-b", "password-value", "developer")

    def tearDown(self) -> None:
        """Removes the isolated relationship database."""

        self.temporary_directory.cleanup()

    def payload(self, iid: str = "42"):
        """Returns one normalized MR-derived Cycle payload."""

        return {
            "schema_version": 1,
            "project": "ExampleApp",
            "mr_iid": iid,
            "mr_url": "https://git.example.com/mobile/example-app/merge_requests/" + iid,
            "target_branch": "main",
            "base_sha": "a" * 40,
            "head_sha": "b" * 40,
        }

    def test_two_users_share_one_cycle_and_relationships_are_idempotent(self) -> None:
        """Creates one Cycle, one relationship per user, and scoped developer lists."""

        first, reused, existed = self.store.create_or_get_cycle_for_user(
            self.payload(), self.user_a["id"]
        )
        second, second_reused, second_existed = self.store.create_or_get_cycle_for_user(
            self.payload(), self.user_b["id"]
        )
        third, third_reused, third_existed = self.store.create_or_get_cycle_for_user(
            self.payload(), self.user_a["id"]
        )
        self.assertFalse(reused)
        self.assertFalse(existed)
        self.assertTrue(second_reused)
        self.assertFalse(second_existed)
        self.assertTrue(third_reused)
        self.assertTrue(third_existed)
        self.assertEqual({first["id"], second["id"], third["id"]}, {first["id"]})
        self.assertEqual([item["id"] for item in self.store.list_cycles_for_user(self.user_a["id"])], [first["id"]])
        self.assertEqual([item["id"] for item in self.store.list_cycles_for_user(self.user_b["id"])], [first["id"]])
        admin_cycle = self.store.list_cycles_with_submitters()[0]
        self.assertEqual(
            {item["username"] for item in admin_cycle["submitters"]},
            {"developer-a", "developer-b"},
        )

    def test_historical_cycle_is_hidden_until_user_submits_it(self) -> None:
        """Keeps a relation-free legacy Cycle admin-only until explicit Web submission."""

        historical = self.store.create_or_get_cycle(self.payload("99"))[0]
        self.assertEqual(self.store.list_cycles_for_user(self.user_a["id"]), [])
        reused, was_reused, relationship_existed = self.store.create_or_get_cycle_for_user(
            self.payload("99"), self.user_a["id"]
        )
        self.assertTrue(was_reused)
        self.assertFalse(relationship_existed)
        self.assertEqual(reused["id"], historical["id"])
        self.assertEqual(self.store.list_cycles_for_user(self.user_a["id"])[0]["id"], historical["id"])

    def test_relationship_failure_rolls_back_new_cycle(self) -> None:
        """Prevents a new Cycle from surviving when the submitter insert aborts."""

        with sqlite3.connect(str(self.database)) as connection:
            connection.execute(
                """
                CREATE TRIGGER reject_cycle_submitter
                BEFORE INSERT ON cycle_submitters
                BEGIN SELECT RAISE(ABORT, 'injected_relationship_failure'); END
                """
            )
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.create_or_get_cycle_for_user(self.payload("77"), self.user_a["id"])
        with sqlite3.connect(str(self.database)) as connection:
            cycle_count = connection.execute(
                "SELECT COUNT(*) FROM coverage_cycles WHERE mr_iid = '77'"
            ).fetchone()[0]
            relationship_count = connection.execute(
                "SELECT COUNT(*) FROM cycle_submitters"
            ).fetchone()[0]
        self.assertEqual((cycle_count, relationship_count), (0, 0))


if __name__ == "__main__":
    unittest.main()
