"""ArtifactFileMixin responsibility extracted from the stable storage facade."""

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


class ArtifactFileMixin:
    def _write_verified_stream(
        self,
        source: BinaryIO,
        destination: Path,
        expected_sha256: str,
        expected_size: Optional[int],
        maximum_bytes: int,
    ) -> int:
        """Streams one upload to a private path, fsyncs, and verifies size and SHA-256."""

        with destination.open("xb") as output:
            total, actual_sha256 = self._stream_and_hash(source, output, maximum_bytes)
            output.flush()
            os.fsync(output.fileno())
        self._validate_stream_result(total, actual_sha256, expected_sha256, expected_size)
        return total
    def _verify_stream(
        self,
        source: BinaryIO,
        expected_sha256: str,
        expected_size: Optional[int],
        maximum_bytes: int,
    ) -> int:
        """Consumes a replayed upload and proves its actual bytes still match the declaration."""

        total, actual_sha256 = self._stream_and_hash(source, None, maximum_bytes)
        self._validate_stream_result(total, actual_sha256, expected_sha256, expected_size)
        return total
    def _stream_and_hash(
        self, source: BinaryIO, output: Optional[BinaryIO], maximum_bytes: int
    ) -> Tuple[int, str]:
        """Reads a bounded stream once, optionally copying it while computing SHA-256."""

        digest = hashlib.sha256()
        total = 0
        while True:
            chunk = source.read(1024 * 1024)
            if not chunk:
                break
            total += len(chunk)
            if total > maximum_bytes:
                raise CoverageStoreError("file_too_large", "uploaded file exceeds its configured limit")
            if output is not None:
                output.write(chunk)
            digest.update(chunk)
        return total, digest.hexdigest()
    def _validate_stream_result(
        self,
        total: int,
        actual_sha256: str,
        expected_sha256: str,
        expected_size: Optional[int],
    ) -> None:
        """Applies common non-empty, size, and checksum requirements to streamed bytes."""

        if total <= 0:
            raise CoverageStoreError("empty_file", "uploaded file must not be empty")
        if expected_size is not None and total != expected_size:
            raise CoverageStoreError("size_mismatch", "uploaded file size does not match manifest")
        if not secrets.compare_digest(actual_sha256, expected_sha256):
            raise CoverageStoreError("sha256_mismatch", "uploaded file SHA-256 does not match declaration")
    def _write_bytes(self, destination: Path, payload: bytes) -> None:
        """Writes and fsyncs one staging metadata file before directory publication."""

        with destination.open("xb") as output:
            output.write(payload)
            output.flush()
            os.fsync(output.fileno())
    def _make_staging_directory(self, kind: str) -> Path:
        """Creates a server-owned staging directory beneath the configured data root."""

        staging_root = self.data_dir / ".staging"
        staging_root.mkdir(parents=True, exist_ok=True)
        return Path(tempfile.mkdtemp(prefix="." + kind + "-", dir=str(staging_root)))
    def _build_directory(self, cycle_id: str, build_id: str) -> Path:
        """Returns the server-generated final Build directory."""

        return self.data_dir / "cycles" / cycle_id / "builds" / build_id
    def _run_directory(self, cycle_id: str, run_id: str) -> Path:
        """Returns the server-generated final Run directory independent of Build readiness."""

        return self.data_dir / "cycles" / cycle_id / "runs" / run_id
    def _fsync_directory(self, directory: Path) -> None:
        """Flushes a directory entry after an atomic rename on macOS."""

        descriptor = os.open(str(directory), os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
