"""ValidationMixin responsibility extracted from the stable storage facade."""

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


class ValidationMixin:
    def _require_schema_version(self, payload: Mapping[str, object]) -> None:
        """Accepts only the explicit Build schema version."""

        if payload.get("schema_version") != 1:
            raise CoverageStoreError("unsupported_schema_version", "schema_version must equal 1")
    def _require_text(self, payload: Mapping[str, object], field_name: str) -> str:
        """Returns one bounded non-empty string field."""

        value = payload.get(field_name)
        if not isinstance(value, str) or not value.strip() or value != value.strip():
            raise CoverageStoreError("invalid_field", field_name + " must be a non-empty string")
        if len(value) > IDENTIFIER_MAX_LENGTH:
            raise CoverageStoreError("invalid_field", field_name + " is too long")
        return value
    def _optional_text(self, payload: Mapping[str, object], field_name: str) -> Optional[str]:
        """Returns a bounded optional string without accepting non-string values."""

        value = payload.get(field_name)
        if value is None:
            return None
        return self._require_text(payload, field_name)
    def _require_git_sha(self, payload: Mapping[str, object], field_name: str) -> str:
        """Requires a full SHA-1 or SHA-256 Git object identifier."""

        value = self._require_text(payload, field_name).lower()
        if not GIT_SHA_PATTERN.fullmatch(value):
            raise CoverageStoreError("invalid_git_sha", field_name + " must be a full Git SHA")
        return value
    def _require_uuid_field(self, payload: Mapping[str, object], field_name: str) -> str:
        """Requires and normalizes one UUID identity field."""

        return self._normalize_uuid(payload.get(field_name), field_name)
    def _require_uuid_value(self, value: str, field_name: str) -> str:
        """Validates a UUID supplied in an API path."""

        return self._normalize_uuid(value, field_name)
    def _normalize_uuid(self, value: object, field_name: str) -> str:
        """Normalizes UUID text to lowercase hyphenated form."""

        if not isinstance(value, str):
            raise CoverageStoreError("invalid_uuid", field_name + " must be a UUID string")
        try:
            return str(uuid.UUID(value))
        except ValueError:
            raise CoverageStoreError("invalid_uuid", field_name + " must be a valid UUID")
    def _require_timestamp(self, value: str, field_name: str) -> None:
        """Requires an ISO-8601 timestamp with an explicit timezone."""

        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            raise CoverageStoreError("invalid_timestamp", field_name + " must be ISO-8601")
        if parsed.tzinfo is None:
            raise CoverageStoreError("invalid_timestamp", field_name + " must include a timezone")
    def _reject_unknown_fields(self, payload: Mapping[str, object], allowed: set) -> None:
        """Rejects protocol drift instead of silently discarding misspelled fields."""

        unknown = sorted(set(payload) - allowed)
        if unknown:
            raise CoverageStoreError("unknown_field", "unknown field(s): " + ", ".join(unknown))
    def _canonical_json_bytes(self, payload: Mapping[str, object]) -> bytes:
        """Serializes semantic JSON deterministically for idempotency fingerprints."""

        return json.dumps(
            payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
    def _utc_now(self) -> str:
        """Returns one timezone-explicit UTC timestamp."""

        return datetime.now(timezone.utc).isoformat()
