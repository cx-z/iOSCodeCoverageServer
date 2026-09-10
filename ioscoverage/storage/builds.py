"""BuildStoreMixin responsibility extracted from the stable storage facade."""

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


class BuildStoreMixin:
    def register_build(
        self,
        manifest: Mapping[str, object],
        artifacts: Mapping[str, Tuple[str, BinaryIO]],
        max_artifact_bytes: int,
    ) -> Tuple[Dict[str, object], bool]:
        """Validates and atomically persists one exact Build artifact set."""

        normalized = self._validate_build_manifest(manifest)
        build_id = str(normalized["build_id"])
        cycle_id = str(normalized["cycle_id"])
        artifact_fields = tuple(normalized["artifacts"])  # type: ignore[arg-type]
        self._validate_uploaded_artifact_set(artifacts, artifact_fields)
        manifest_bytes = self._canonical_json_bytes(manifest)
        manifest_sha256 = hashlib.sha256(manifest_bytes).hexdigest()
        with self._mutation_lock:
            cycle = self.get_cycle(cycle_id)
            definition = self.project_definition(cycle["project"])
            if normalized["bundle_id"] not in definition.allowed_bundle_ids:
                raise CoverageStoreError(
                    "bundle_id_not_allowed", "bundle_id is not allowed for the Cycle project"
                )
            existing = self._find_build(build_id)
            if existing is not None:
                if existing["cycle_id"] == cycle_id and existing["manifest_sha256"] == manifest_sha256:
                    self._validate_build_git_snapshot(normalized, definition.project)
                    for field_name in artifact_fields:
                        uploaded_filename, stream = artifacts[field_name]
                        descriptor = normalized["artifacts"][field_name]  # type: ignore[index]
                        if Path(uploaded_filename).name != descriptor["filename"]:  # type: ignore[index]
                            raise CoverageStoreError(
                                "artifact_filename_mismatch",
                                field_name + " filename does not match its manifest descriptor",
                            )
                        self._verify_stream(
                            stream,
                            str(descriptor["sha256"]),  # type: ignore[index]
                            int(descriptor["size"]),  # type: ignore[index]
                            max_artifact_bytes,
                        )
                    self._assert_build_files_exist(cycle_id, build_id, artifact_fields)
                    return self.get_build(build_id), True
                raise CoverageStoreError(
                    "build_id_conflict",
                    "build_id is already registered with different manifest content",
                )

            self._validate_build_git_snapshot(normalized, definition.project)
            staging_dir = self._make_staging_directory("build")
            final_dir = self._build_directory(cycle_id, build_id)
            try:
                self._write_bytes(staging_dir / "manifest.json", manifest_bytes + b"\n")
                artifact_dir = staging_dir / "artifacts"
                artifact_dir.mkdir()
                artifact_paths: Dict[str, Path] = {}
                for field_name in artifact_fields:
                    uploaded_filename, stream = artifacts[field_name]
                    descriptor = normalized["artifacts"][field_name]  # type: ignore[index]
                    if Path(uploaded_filename).name != descriptor["filename"]:  # type: ignore[index]
                        raise CoverageStoreError(
                            "artifact_filename_mismatch",
                            field_name + " filename does not match its manifest descriptor",
                        )
                    destination = artifact_dir / ARTIFACT_STORAGE_NAMES[field_name]
                    self._write_verified_stream(
                        stream,
                        destination,
                        str(descriptor["sha256"]),  # type: ignore[index]
                        int(descriptor["size"]),  # type: ignore[index]
                        max_artifact_bytes,
                    )
                    artifact_paths[field_name] = destination
                try:
                    actual_uuids = self.artifact_validator.validate(
                        artifact_paths,
                        normalized["macho_uuids"],  # type: ignore[arg-type]
                        str(normalized["coverage_binary"]),
                        staging_dir,
                        max_artifact_bytes,
                    )
                except ArtifactValidationError as error:
                    raise CoverageStoreError(error.code, error.message) from error
                normalized["macho_uuids"] = actual_uuids
                final_dir.parent.mkdir(parents=True, exist_ok=True)
                if final_dir.exists():
                    raise CoverageStoreError(
                        "storage_conflict", "Build storage path already exists without metadata"
                    )
                os.replace(str(staging_dir), str(final_dir))
                self._fsync_directory(final_dir.parent)
                try:
                    self._insert_build(normalized, manifest_sha256, manifest_bytes.decode("utf-8"))
                except Exception:
                    shutil.rmtree(str(final_dir), ignore_errors=True)
                    raise
            finally:
                if staging_dir.exists():
                    shutil.rmtree(str(staging_dir), ignore_errors=True)
        return self.get_build(build_id), False
    def get_build(self, build_id: str) -> Dict[str, object]:
        """Returns one registered Build with parsed manifest and artifact metadata."""

        build_id = self._require_uuid_value(build_id, "build_id")
        row = self._find_build(build_id)
        if row is None:
            raise CoverageStoreError("build_not_found", "build_id is not registered")
        result = dict(row)
        manifest = json.loads(str(result.pop("manifest_json")))
        result["manifest"] = manifest
        main_uuids = json.loads(str(result.pop("main_executable_uuids_json")))
        debug_uuids_json = result.pop("debug_dylib_uuids_json")
        dsym_uuids = json.loads(str(result.pop("dsym_uuids_json")))
        result["main_executable_uuids"] = main_uuids
        result["dsym_uuids"] = dsym_uuids
        result["artifacts"] = manifest["artifacts"]
        result["macho_uuids"] = {
            "main_executable": main_uuids,
            "dsym": dsym_uuids,
        }
        if debug_uuids_json is not None:
            debug_uuids = json.loads(str(debug_uuids_json))
            result["debug_dylib_uuids"] = debug_uuids
            result["macho_uuids"]["debug_dylib"] = debug_uuids
        else:
            result.pop("debug_dylib_sha256", None)
            result.pop("debug_dylib_size", None)
        result["pending_runs_promoted"] = int(result.get("pending_runs_promoted", 0))
        return result
    def _insert_build(
        self, normalized: Mapping[str, object], manifest_sha256: str, manifest_json: str
    ) -> None:
        """Commits Build metadata and promotes matching pending Runs in one transaction."""

        artifacts = normalized["artifacts"]  # type: ignore[assignment]
        macho_uuids = normalized["macho_uuids"]  # type: ignore[assignment]
        debug_artifact = artifacts.get("debug_dylib")
        debug_uuids = macho_uuids.get("debug_dylib")
        with self._database() as connection:
            connection.execute("BEGIN IMMEDIATE")
            promoted = connection.execute(
                """
                UPDATE runs SET state = 'uploaded', analyzable = 1, error_message = NULL
                WHERE cycle_id = ? AND build_id = ? AND state = 'pending_build'
                """,
                (normalized["cycle_id"], normalized["build_id"]),
            ).rowcount
            connection.execute(
                """
                INSERT INTO builds (
                    id, cycle_id, schema_version, git_sha, target_sha_at_build,
                    merge_base_sha, coverage_binary, bundle_id, app_version,
                    bundle_version, xcode_version, swift_version,
                    main_executable_uuids_json, debug_dylib_uuids_json, dsym_uuids_json,
                    manifest_sha256, manifest_json,
                    main_executable_sha256, main_executable_size,
                    debug_dylib_sha256, debug_dylib_size, dsym_sha256, dsym_size,
                    artifact_state, pending_runs_promoted, created_at
                ) VALUES (?, ?, 1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'ready', ?, ?)
                """,
                (
                    normalized["build_id"],
                    normalized["cycle_id"],
                    normalized["git_sha"],
                    normalized["target_sha_at_build"],
                    normalized["merge_base_sha"],
                    normalized["coverage_binary"],
                    normalized["bundle_id"],
                    normalized["app_version"],
                    normalized["bundle_version"],
                    normalized["xcode_version"],
                    normalized["swift_version"],
                    json.dumps(macho_uuids["main_executable"], separators=(",", ":")),
                    json.dumps(debug_uuids, separators=(",", ":"))
                    if debug_uuids is not None
                    else None,
                    json.dumps(macho_uuids["dsym"], separators=(",", ":")),
                    manifest_sha256,
                    manifest_json,
                    artifacts["main_executable"]["sha256"],
                    artifacts["main_executable"]["size"],
                    debug_artifact["sha256"] if debug_artifact is not None else None,
                    debug_artifact["size"] if debug_artifact is not None else None,
                    artifacts["dsym"]["sha256"],
                    artifacts["dsym"]["size"],
                    promoted,
                    self._utc_now(),
                ),
            )
    def _validate_build_manifest(self, manifest: Mapping[str, object]) -> Dict[str, object]:
        """Validates the stable Build identity and declared artifact shape."""

        allowed = {
            "schema_version",
            "build_id",
            "cycle_id",
            "git_sha",
            "target_sha_at_build",
            "merge_base_sha",
            "coverage_binary",
            "bundle_id",
            "app_version",
            "bundle_version",
            "xcode_version",
            "swift_version",
            "macho_uuids",
            "artifacts",
        }
        self._reject_unknown_fields(manifest, allowed)
        missing = sorted(allowed - set(manifest))
        if missing:
            raise CoverageStoreError(
                "missing_build_field", "missing required Build field(s): " + ", ".join(missing)
            )
        self._require_schema_version(manifest)
        coverage_binary = manifest.get("coverage_binary")
        if coverage_binary not in ("main_executable", "debug_dylib"):
            raise CoverageStoreError(
                "invalid_coverage_binary",
                "coverage_binary must be main_executable or debug_dylib",
            )
        normalized: Dict[str, object] = {
            "build_id": self._require_uuid_field(manifest, "build_id"),
            "cycle_id": self._require_uuid_field(manifest, "cycle_id"),
            "git_sha": self._require_git_sha(manifest, "git_sha"),
            "target_sha_at_build": self._require_git_sha(manifest, "target_sha_at_build"),
            "merge_base_sha": self._require_git_sha(manifest, "merge_base_sha"),
            "coverage_binary": coverage_binary,
            "bundle_id": self._require_text(manifest, "bundle_id"),
            "app_version": self._require_text(manifest, "app_version"),
            "bundle_version": self._require_text(manifest, "bundle_version"),
            "xcode_version": self._require_text(manifest, "xcode_version"),
            "swift_version": self._require_text(manifest, "swift_version"),
        }
        raw_uuids = manifest.get("macho_uuids")
        raw_artifacts = manifest.get("artifacts")
        if not isinstance(raw_uuids, dict) or not isinstance(raw_artifacts, dict):
            raise CoverageStoreError(
                "invalid_manifest", "macho_uuids and artifacts must be JSON objects"
            )
        artifact_fields = set(raw_artifacts)
        if (
            set(raw_uuids) != artifact_fields
            or not set(REQUIRED_ARTIFACT_FIELDS).issubset(artifact_fields)
            or not artifact_fields.issubset(set(ARTIFACT_FIELDS))
        ):
            raise CoverageStoreError(
                "invalid_artifact_set",
                "macho_uuids and artifacts must describe the same allowed artifact set",
            )
        if coverage_binary == "debug_dylib" and "debug_dylib" not in artifact_fields:
            raise CoverageStoreError(
                "coverage_binary_artifact_missing",
                "debug_dylib must be declared when it is the coverage_binary",
            )
        normalized_uuids: Dict[str, List[str]] = {}
        for field_name in artifact_fields:
            values = raw_uuids[field_name]
            if not isinstance(values, list) or not values:
                raise CoverageStoreError("invalid_manifest", field_name + " UUID list is required")
            normalized_uuids[field_name] = [
                self._normalize_uuid(value, field_name + " UUID") for value in values
            ]
        normalized["macho_uuids"] = normalized_uuids

        normalized_artifacts: Dict[str, Dict[str, object]] = {}
        for field_name in artifact_fields:
            descriptor = raw_artifacts[field_name]
            if not isinstance(descriptor, dict):
                raise CoverageStoreError("invalid_manifest", field_name + " descriptor is required")
            allowed_descriptor = {"filename", "sha256", "size"}
            if field_name == "dsym":
                allowed_descriptor.add("archive_format")
            self._reject_unknown_fields(descriptor, allowed_descriptor)
            filename = self._require_text(descriptor, "filename")
            if (Path(filename).name != filename or filename in (".", "..")
                    or "\\" in filename or "\x00" in filename):
                raise CoverageStoreError("invalid_manifest", "artifact filename must be a basename")
            if field_name == "dsym":
                if descriptor.get("archive_format") != "zip" or not filename.endswith(".zip"):
                    raise CoverageStoreError(
                        "invalid_manifest", "dSYM must be supplied as a zip archive"
                    )
            sha256 = str(descriptor.get("sha256", "")).lower()
            if not SHA256_PATTERN.fullmatch(sha256):
                raise CoverageStoreError("invalid_manifest", field_name + " has invalid sha256")
            size = descriptor.get("size")
            if not isinstance(size, int) or isinstance(size, bool) or size <= 0:
                raise CoverageStoreError("invalid_manifest", field_name + " size must be positive")
            normalized_artifacts[field_name] = {
                "filename": filename,
                "sha256": sha256,
                "size": size,
            }
        normalized["artifacts"] = normalized_artifacts
        return normalized
    def _validate_uploaded_artifact_set(
        self,
        artifacts: Mapping[str, Tuple[str, BinaryIO]],
        manifest_fields: Tuple[str, ...],
    ) -> None:
        """Requires multipart files and manifest descriptors to name the same artifacts."""

        if set(artifacts) != set(manifest_fields):
            raise CoverageStoreError(
                "artifact_set_mismatch",
                "multipart files must match manifest artifacts exactly",
            )
    def _validate_build_git_snapshot(self, normalized: Mapping[str, object], project: object) -> None:
        """Delegates validation to the Cycle project's trusted repository."""

        try:
            self.project_registry.repository(project).validate_build_snapshot(
                str(normalized["git_sha"]),
                str(normalized["target_sha_at_build"]),
                str(normalized["merge_base_sha"]),
            )
        except GitRepositoryError as error:
            raise CoverageStoreError(error.code, error.message) from error
    def _assert_build_files_exist(
        self, cycle_id: str, build_id: str, artifact_fields: Tuple[str, ...]
    ) -> None:
        """Prevents a duplicate response from hiding missing immutable Build artifacts."""

        base = self._build_directory(cycle_id, build_id)
        if not (base / "manifest.json").is_file():
            raise CoverageStoreError("stored_file_missing", "registered Build artifact is missing")
        row = self._find_build(build_id)
        evidence_store = getattr(self, "retained_evidence_store", None)
        for field_name in artifact_fields:
            path = base / "artifacts" / ARTIFACT_STORAGE_NAMES[field_name]
            if path.is_file():
                continue
            if row is not None and evidence_store is not None and evidence_store.build_artifact_is_retained(
                cycle_id, build_id, row["manifest_sha256"], field_name
            ):
                continue
            raise CoverageStoreError("stored_file_missing", "registered Build artifact is missing")
    def _find_build(self, build_id: str) -> Optional[sqlite3.Row]:
        """Reads Build metadata without converting JSON columns."""

        with self._database() as connection:
            return connection.execute("SELECT * FROM builds WHERE id = ?", (build_id,)).fetchone()
