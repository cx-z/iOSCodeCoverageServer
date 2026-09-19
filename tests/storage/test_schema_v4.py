#!/usr/bin/env python3
"""Focused checks for the additive coverage database v4 migration."""

import sqlite3
import subprocess
import tempfile
import unittest
from pathlib import Path

from ioscoverage.integrations.xcode_artifacts import XcodeArtifactValidator
from ioscoverage.storage.analysis_state import AnalysisStateStore
from ioscoverage.storage.common import CoverageStoreError
from ioscoverage.storage.coverage_store import CoverageStore
from tests.helpers import project_registry


class SchemaV4MigrationTests(unittest.TestCase):
    """Proves v4 adds identity tables without changing pre-existing business rows."""

    def setUp(self) -> None:
        """Creates one isolated registered project and runtime database path."""

        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)
        self.repository = self.root / "ExampleApp"
        subprocess.run(
            ["git", "init", str(self.repository)],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        self.database = self.root / "instance" / "coverage.sqlite3"
        self.data_dir = self.root / "data"

    def tearDown(self) -> None:
        """Removes the isolated migration fixture."""

        self.temporary_directory.cleanup()

    def make_store(self) -> CoverageStore:
        """Initializes the production schema against the temporary database."""

        return CoverageStore(
            self.database,
            self.data_dir,
            project_registry(self.repository),
            XcodeArtifactValidator(Path("/Applications/Xcode263.app/Contents/Developer")),
        )

    def test_empty_database_initializes_directly_at_v4_and_is_idempotent(self) -> None:
        """Creates all account tables and permits a repeated initialization."""

        self.make_store()
        self.make_store()
        with sqlite3.connect(str(self.database)) as connection:
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            tables = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                )
            }
        self.assertEqual(version, 4)
        self.assertTrue(
            {"platform_users", "web_sessions", "cycle_submitters"}.issubset(tables)
        )

    def test_v3_database_upgrades_without_changing_coverage_or_analysis_rows(self) -> None:
        """Preserves representative Cycle, Build, Run, and analysis rows byte-for-byte."""

        store = self.make_store()
        cycle = store.create_cycle(
            {
                "schema_version": 1,
                "project": "ExampleApp",
                "mr_iid": "42",
                "mr_url": "https://git.example.com/mobile/example-app/merge_requests/42",
                "target_branch": "main",
                "base_sha": "a" * 40,
                "head_sha": "b" * 40,
            }
        )
        state_store = AnalysisStateStore(self.database)
        state_store.request_analysis(cycle["id"], "manual_retry")
        with sqlite3.connect(str(self.database)) as connection:
            connection.execute(
                """
                INSERT INTO builds (
                    id, cycle_id, schema_version, git_sha, target_sha_at_build,
                    merge_base_sha, coverage_binary, bundle_id, app_version,
                    bundle_version, xcode_version, swift_version,
                    main_executable_uuids_json, debug_dylib_uuids_json,
                    dsym_uuids_json, manifest_sha256, manifest_json,
                    main_executable_sha256, main_executable_size,
                    debug_dylib_sha256, debug_dylib_size, dsym_sha256, dsym_size,
                    artifact_state, pending_runs_promoted, created_at
                ) VALUES (?, ?, 1, ?, ?, ?, 'main_executable', ?, '1.0', '1',
                          '26.3', '6.2', '[]', NULL, '[]', ?, '{}', ?, 4,
                          NULL, NULL, ?, 4, 'ready', 0, ?)
                """,
                (
                    "build-fixture",
                    cycle["id"],
                    "b" * 40,
                    "c" * 40,
                    "a" * 40,
                    "com.example.fixture",
                    "1" * 64,
                    "2" * 64,
                    "3" * 64,
                    "2026-01-01T00:00:00+00:00",
                ),
            )
            connection.execute(
                """
                INSERT INTO runs (
                    id, cycle_id, build_id, schema_version, device_id, process_id,
                    launch_id, captured_at, received_at, metadata_sha256,
                    metadata_json, payload_sha256, payload_size, state, analyzable,
                    error_message
                ) VALUES (?, ?, ?, 1, ?, '7', ?, ?, ?, ?, '{}', ?, 4,
                          'uploaded', 1, NULL)
                """,
                (
                    "run-fixture",
                    cycle["id"],
                    "build-fixture",
                    "device-fixture",
                    "launch-fixture",
                    "2026-01-01T00:01:00+00:00",
                    "2026-01-01T00:02:00+00:00",
                    "4" * 64,
                    "5" * 64,
                ),
            )
            connection.execute("PRAGMA user_version = 3")
            before = {
                table: connection.execute("SELECT * FROM " + table).fetchall()
                for table in ("coverage_cycles", "builds", "runs", "cycle_analysis_states")
            }
        self.make_store()
        with sqlite3.connect(str(self.database)) as connection:
            after = {
                table: connection.execute("SELECT * FROM " + table).fetchall()
                for table in ("coverage_cycles", "builds", "runs", "cycle_analysis_states")
            }
            version = connection.execute("PRAGMA user_version").fetchone()[0]
        self.assertEqual(before, after)
        self.assertEqual(version, 4)

    def test_future_database_version_is_rejected_without_mutation(self) -> None:
        """Fails closed before applying schema statements to an unknown future database."""

        self.database.parent.mkdir(parents=True)
        with sqlite3.connect(str(self.database)) as connection:
            connection.execute("CREATE TABLE sentinel (value TEXT NOT NULL)")
            connection.execute("INSERT INTO sentinel VALUES ('unchanged')")
            connection.execute("PRAGMA user_version = 99")
        with self.assertRaisesRegex(CoverageStoreError, "newer than this server"):
            self.make_store()
        with sqlite3.connect(str(self.database)) as connection:
            self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 99)
            self.assertEqual(connection.execute("SELECT value FROM sentinel").fetchone()[0], "unchanged")
            account_table = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'platform_users'"
            ).fetchone()
        self.assertIsNone(account_table)


if __name__ == "__main__":
    unittest.main()
