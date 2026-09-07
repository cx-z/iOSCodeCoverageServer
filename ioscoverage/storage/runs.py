"""RunStoreMixin responsibility extracted from the stable storage facade."""

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


class RunStoreMixin:
    def save_run(
        self,
        metadata: Mapping[str, object],
        declared_sha256: str,
        filename: str,
        profile_stream: BinaryIO,
        max_profile_bytes: int,
    ) -> Tuple[Dict[str, object], bool]:
        """Persists one independent profraw with strict run_id idempotency."""

        normalized = self._validate_run_metadata(metadata)
        declared_sha256 = declared_sha256.lower()
        if not SHA256_PATTERN.fullmatch(declared_sha256):
            raise CoverageStoreError("invalid_sha256", "sha256 must be 64 lowercase hex digits")
        run_id = str(normalized["run_id"])
        cycle_id = str(normalized["cycle_id"])
        build_id = str(normalized["build_id"])
        if Path(filename).name.lower() != run_id + ".profraw":
            raise CoverageStoreError(
                "invalid_profile_filename", "profile filename must be <run_id>.profraw"
            )
        metadata_bytes = self._canonical_json_bytes(metadata)
        metadata_sha256 = hashlib.sha256(metadata_bytes).hexdigest()

        with self._mutation_lock:
            self.get_cycle(cycle_id)
            registered_build = self._find_build(build_id)
            if registered_build is not None and registered_build["cycle_id"] != cycle_id:
                raise CoverageStoreError(
                    "build_cycle_mismatch",
                    "build_id is registered under a different cycle_id",
                )
            existing = self._find_run(run_id)
            if existing is not None:
                replay_size = self._verify_stream(
                    profile_stream, declared_sha256, None, max_profile_bytes
                )
                identical = (
                    existing["cycle_id"] == cycle_id
                    and existing["build_id"] == build_id
                    and existing["payload_sha256"] == declared_sha256
                    and existing["payload_size"] == replay_size
                    and existing["metadata_sha256"] == metadata_sha256
                )
                if not identical:
                    raise CoverageStoreError(
                        "run_id_conflict",
                        "run_id is already stored with different metadata or profile content",
                    )
                self._assert_run_files_exist(cycle_id, run_id)
                return self.get_run(run_id), True

            staging_dir = self._make_staging_directory("run")
            final_dir = self._run_directory(cycle_id, run_id)
            try:
                self._write_bytes(staging_dir / "metadata.json", metadata_bytes + b"\n")
                actual_size = self._write_verified_stream(
                    profile_stream,
                    staging_dir / "payload.profraw",
                    declared_sha256,
                    None,
                    max_profile_bytes,
                )
                self._write_bytes(staging_dir / "upload.sha256", (declared_sha256 + "\n").encode("ascii"))
                final_dir.parent.mkdir(parents=True, exist_ok=True)
                if final_dir.exists():
                    raise CoverageStoreError(
                        "storage_conflict", "Run storage path already exists without metadata"
                    )
                os.replace(str(staging_dir), str(final_dir))
                self._fsync_directory(final_dir.parent)
                try:
                    self._insert_run(normalized, metadata_sha256, metadata_bytes.decode("utf-8"), declared_sha256, actual_size)
                except Exception:
                    shutil.rmtree(str(final_dir), ignore_errors=True)
                    raise
            finally:
                if staging_dir.exists():
                    shutil.rmtree(str(staging_dir), ignore_errors=True)
        return self.get_run(run_id), False
    def get_run(self, run_id: str) -> Dict[str, object]:
        """Returns one Run without exposing local filesystem paths."""

        run_id = self._require_uuid_value(run_id, "run_id")
        row = self._find_run(run_id)
        if row is None:
            raise CoverageStoreError("run_not_found", "run_id is not stored")
        result = dict(row)
        result["metadata"] = json.loads(str(result.pop("metadata_json")))
        result["analyzable"] = bool(result["analyzable"])
        return result
    def _insert_run(
        self,
        normalized: Mapping[str, object],
        metadata_sha256: str,
        metadata_json: str,
        payload_sha256: str,
        payload_size: int,
    ) -> None:
        """Commits Run state based only on whether its exact Build is ready."""

        build = self._find_build(str(normalized["build_id"]))
        build_registered = build is not None and build["cycle_id"] == normalized["cycle_id"]
        state = "uploaded" if build_registered else "pending_build"
        analyzable = 1 if build_registered else 0
        error_message = None if build_registered else "build_not_registered"
        with self._database() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                INSERT INTO runs (
                    id, cycle_id, build_id, schema_version, device_id, process_id,
                    launch_id, captured_at, received_at, metadata_sha256, metadata_json,
                    payload_sha256, payload_size, state, analyzable, error_message
                ) VALUES (?, ?, ?, 1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    normalized["run_id"],
                    normalized["cycle_id"],
                    normalized["build_id"],
                    normalized["device_id"],
                    str(normalized["process_id"]),
                    normalized["launch_id"],
                    normalized["captured_at"],
                    self._utc_now(),
                    metadata_sha256,
                    metadata_json,
                    payload_sha256,
                    payload_size,
                    state,
                    analyzable,
                    error_message,
                ),
            )
    def _validate_run_metadata(self, metadata: Mapping[str, object]) -> Dict[str, object]:
        """Validates required Run identity while retaining allowed diagnostic metadata."""

        allowed = {
            "schema_version",
            "run_id",
            "cycle_id",
            "build_id",
            "device_id",
            "process_id",
            "launch_id",
            "captured_at",
            "app_state",
            "trigger_reason",
        }
        self._reject_unknown_fields(metadata, allowed)
        self._require_schema_version(metadata)
        process_id = metadata.get("process_id")
        if not isinstance(process_id, int) or isinstance(process_id, bool) or process_id <= 0:
            raise CoverageStoreError("invalid_metadata", "process_id must be a positive integer")
        captured_at = self._require_text(metadata, "captured_at")
        self._require_timestamp(captured_at, "captured_at")
        normalized: Dict[str, object] = {
            "run_id": self._require_uuid_field(metadata, "run_id"),
            "cycle_id": self._require_uuid_field(metadata, "cycle_id"),
            "build_id": self._require_uuid_field(metadata, "build_id"),
            "device_id": self._require_uuid_field(metadata, "device_id"),
            "process_id": process_id,
            "launch_id": self._require_uuid_field(metadata, "launch_id"),
            "captured_at": captured_at,
        }
        for optional_field in ("app_state", "trigger_reason"):
            if optional_field in metadata:
                normalized[optional_field] = self._require_text(metadata, optional_field)
        return normalized
    def _assert_run_files_exist(self, cycle_id: str, run_id: str) -> None:
        """Prevents an idempotent response from hiding missing immutable Run files."""

        base = self._run_directory(cycle_id, run_id)
        if not all(
            path.is_file()
            for path in (base / "metadata.json", base / "payload.profraw", base / "upload.sha256")
        ):
            raise CoverageStoreError("stored_file_missing", "stored Run payload is missing")
    def _find_run(self, run_id: str) -> Optional[sqlite3.Row]:
        """Reads Run metadata without converting JSON columns."""

        with self._database() as connection:
            return connection.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
