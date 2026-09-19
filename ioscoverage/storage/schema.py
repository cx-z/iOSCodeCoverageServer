"""SchemaMixin responsibility extracted from the stable storage facade."""

import hashlib
import json
import os
import re
import secrets
import shutil
import sqlite3
import tempfile
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import BinaryIO, Dict, Iterator, List, Mapping, Optional, Tuple

from ioscoverage.integrations.xcode_artifacts import ArtifactValidationError
from ioscoverage.integrations.git_repository import GitRepositoryError
from ioscoverage.storage.common import *


DATABASE_USER_VERSION = 4


class SchemaMixin:
    def _initialize_schema(self) -> None:
        """Applies the additive v4 schema without rewriting existing coverage data."""

        with self._database() as connection:
            current_version = int(connection.execute("PRAGMA user_version").fetchone()[0])
            if current_version > DATABASE_USER_VERSION:
                raise CoverageStoreError(
                    "unsupported_database_schema", "coverage database is newer than this server"
                )
            connection.executescript(
                """
                BEGIN IMMEDIATE;
                CREATE TABLE IF NOT EXISTS coverage_cycles (
                    id TEXT PRIMARY KEY,
                    schema_version INTEGER NOT NULL CHECK(schema_version = 1),
                    project TEXT NOT NULL,
                    mr_iid TEXT,
                    mr_url TEXT,
                    target_branch TEXT NOT NULL,
                    base_sha TEXT NOT NULL,
                    head_sha TEXT NOT NULL,
                    status TEXT NOT NULL CHECK(status IN ('active', 'archived')),
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS builds (
                    id TEXT PRIMARY KEY,
                    cycle_id TEXT NOT NULL REFERENCES coverage_cycles(id),
                    schema_version INTEGER NOT NULL CHECK(schema_version = 1),
                    git_sha TEXT NOT NULL,
                    target_sha_at_build TEXT NOT NULL,
                    merge_base_sha TEXT NOT NULL,
                    coverage_binary TEXT NOT NULL
                        CHECK(coverage_binary IN ('main_executable', 'debug_dylib')),
                    bundle_id TEXT NOT NULL,
                    app_version TEXT NOT NULL,
                    bundle_version TEXT NOT NULL,
                    xcode_version TEXT NOT NULL,
                    swift_version TEXT NOT NULL,
                    main_executable_uuids_json TEXT NOT NULL,
                    debug_dylib_uuids_json TEXT,
                    dsym_uuids_json TEXT NOT NULL,
                    manifest_sha256 TEXT NOT NULL,
                    manifest_json TEXT NOT NULL,
                    main_executable_sha256 TEXT NOT NULL,
                    main_executable_size INTEGER NOT NULL,
                    debug_dylib_sha256 TEXT,
                    debug_dylib_size INTEGER,
                    dsym_sha256 TEXT NOT NULL,
                    dsym_size INTEGER NOT NULL,
                    artifact_state TEXT NOT NULL CHECK(artifact_state IN ('ready', 'invalid')),
                    pending_runs_promoted INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS runs (
                    id TEXT PRIMARY KEY,
                    cycle_id TEXT NOT NULL REFERENCES coverage_cycles(id),
                    build_id TEXT NOT NULL,
                    schema_version INTEGER NOT NULL CHECK(schema_version = 1),
                    device_id TEXT NOT NULL,
                    process_id TEXT NOT NULL,
                    launch_id TEXT NOT NULL,
                    captured_at TEXT NOT NULL,
                    received_at TEXT NOT NULL,
                    metadata_sha256 TEXT NOT NULL,
                    metadata_json TEXT NOT NULL,
                    payload_sha256 TEXT NOT NULL,
                    payload_size INTEGER NOT NULL,
                    state TEXT NOT NULL CHECK(state IN ('pending_build', 'uploaded')),
                    analyzable INTEGER NOT NULL CHECK(analyzable IN (0, 1)),
                    error_message TEXT
                );

                CREATE INDEX IF NOT EXISTS builds_cycle_id_index ON builds(cycle_id);
                CREATE INDEX IF NOT EXISTS runs_cycle_id_index ON runs(cycle_id);
                CREATE INDEX IF NOT EXISTS runs_build_id_index ON runs(build_id);
                CREATE UNIQUE INDEX IF NOT EXISTS coverage_cycles_project_mr_unique
                    ON coverage_cycles(project, mr_iid) WHERE mr_iid IS NOT NULL;

                CREATE TABLE IF NOT EXISTS platform_users (
                    id TEXT PRIMARY KEY,
                    username TEXT NOT NULL,
                    username_key TEXT NOT NULL UNIQUE,
                    password_scheme TEXT NOT NULL CHECK(password_scheme = 'scrypt-v1'),
                    password_salt BLOB NOT NULL,
                    password_digest BLOB NOT NULL,
                    role TEXT NOT NULL CHECK(role IN ('developer', 'admin')),
                    status TEXT NOT NULL CHECK(status IN ('active', 'disabled')),
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS web_sessions (
                    token_sha256 TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL REFERENCES platform_users(id),
                    created_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS web_sessions_user_index
                    ON web_sessions(user_id, expires_at);

                CREATE TABLE IF NOT EXISTS cycle_submitters (
                    cycle_id TEXT NOT NULL REFERENCES coverage_cycles(id),
                    user_id TEXT NOT NULL REFERENCES platform_users(id),
                    submitted_at TEXT NOT NULL,
                    PRIMARY KEY (cycle_id, user_id)
                );

                CREATE INDEX IF NOT EXISTS cycle_submitters_user_index
                    ON cycle_submitters(user_id, submitted_at DESC, cycle_id);

                CREATE TRIGGER IF NOT EXISTS coverage_cycles_base_sha_immutable
                BEFORE UPDATE OF base_sha ON coverage_cycles
                WHEN OLD.base_sha <> NEW.base_sha
                BEGIN
                    SELECT RAISE(ABORT, 'base_sha_is_immutable');
                END;

                CREATE TRIGGER IF NOT EXISTS coverage_cycles_id_immutable
                BEFORE UPDATE OF id ON coverage_cycles
                WHEN OLD.id <> NEW.id
                BEGIN
                    SELECT RAISE(ABORT, 'cycle_id_is_immutable');
                END;

                CREATE TRIGGER IF NOT EXISTS builds_comparison_snapshot_immutable
                BEFORE UPDATE OF git_sha, target_sha_at_build, merge_base_sha ON builds
                WHEN OLD.git_sha <> NEW.git_sha
                  OR OLD.target_sha_at_build <> NEW.target_sha_at_build
                  OR OLD.merge_base_sha <> NEW.merge_base_sha
                BEGIN
                    SELECT RAISE(ABORT, 'build_comparison_snapshot_is_immutable');
                END;

                CREATE TRIGGER IF NOT EXISTS builds_artifact_identity_immutable
                BEFORE UPDATE OF coverage_binary, manifest_sha256, manifest_json,
                    main_executable_uuids_json, debug_dylib_uuids_json, dsym_uuids_json,
                    main_executable_sha256, main_executable_size,
                    debug_dylib_sha256, debug_dylib_size, dsym_sha256, dsym_size
                ON builds
                WHEN OLD.coverage_binary IS NOT NEW.coverage_binary
                  OR OLD.manifest_sha256 IS NOT NEW.manifest_sha256
                  OR OLD.manifest_json IS NOT NEW.manifest_json
                  OR OLD.main_executable_uuids_json IS NOT NEW.main_executable_uuids_json
                  OR OLD.debug_dylib_uuids_json IS NOT NEW.debug_dylib_uuids_json
                  OR OLD.dsym_uuids_json IS NOT NEW.dsym_uuids_json
                  OR OLD.main_executable_sha256 IS NOT NEW.main_executable_sha256
                  OR OLD.main_executable_size IS NOT NEW.main_executable_size
                  OR OLD.debug_dylib_sha256 IS NOT NEW.debug_dylib_sha256
                  OR OLD.debug_dylib_size IS NOT NEW.debug_dylib_size
                  OR OLD.dsym_sha256 IS NOT NEW.dsym_sha256
                  OR OLD.dsym_size IS NOT NEW.dsym_size
                BEGIN
                    SELECT RAISE(ABORT, 'build_artifact_identity_is_immutable');
                END;

                PRAGMA user_version = 4;
                COMMIT;
                """
            )
