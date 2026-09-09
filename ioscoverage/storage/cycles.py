"""CycleStoreMixin responsibility extracted from the stable storage facade."""

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


class CycleStoreMixin:
    def create_cycle(self, payload: Mapping[str, object]) -> Dict[str, object]:
        """Creates a cycle with a server-generated immutable ID and fixed base SHA."""

        normalized = self._normalize_cycle_payload(payload)
        self.project_definition(normalized["project"])
        with self._mutation_lock:
            cycle_id = self._insert_cycle(normalized)
        return self.get_cycle(cycle_id)
    def create_or_get_cycle(
        self, payload: Mapping[str, object]
    ) -> Tuple[Dict[str, object], bool]:
        """Atomically reuses one project/MR Cycle or creates it from validated fields."""

        normalized = self._normalize_cycle_payload(payload)
        self.project_definition(normalized["project"])
        mr_iid = normalized["mr_iid"]
        if mr_iid is None:
            raise CoverageStoreError("missing_field", "mr_iid is required")
        with self._mutation_lock:
            with self._database() as connection:
                row = connection.execute(
                    "SELECT id FROM coverage_cycles WHERE project = ? AND mr_iid = ? "
                    "ORDER BY created_at ASC, id ASC LIMIT 1",
                    (normalized["project"], mr_iid),
                ).fetchone()
            if row is not None:
                return self.get_cycle(str(row["id"])), True
            cycle_id = self._insert_cycle(normalized)
            return self.get_cycle(cycle_id), False
    def find_cycle_by_merge_request(
        self, project: object, mr_iid: object
    ) -> Optional[Dict[str, object]]:
        """Returns the stable existing Cycle for one registered project/MR identity."""

        definition = self.project_definition(project)
        if isinstance(mr_iid, bool) or not isinstance(mr_iid, (str, int)):
            raise CoverageStoreError("invalid_mr_iid", "mr_iid is invalid")
        normalized_iid = str(mr_iid)
        if not normalized_iid or len(normalized_iid) > 128:
            raise CoverageStoreError("invalid_mr_iid", "mr_iid is invalid")
        with self._database() as connection:
            row = connection.execute(
                "SELECT id FROM coverage_cycles WHERE project = ? AND mr_iid = ? "
                "ORDER BY created_at ASC, id ASC LIMIT 1",
                (definition.project, normalized_iid),
            ).fetchone()
        return None if row is None else self.get_cycle(str(row["id"]))
    def _normalize_cycle_payload(
        self, payload: Mapping[str, object]
    ) -> Dict[str, Optional[str]]:
        """Validates both legacy and server-derived Cycle fields in one place."""

        allowed = {
            "schema_version",
            "project",
            "mr_iid",
            "mr_url",
            "target_branch",
            "base_sha",
            "head_sha",
        }
        self._reject_unknown_fields(payload, allowed)
        self._require_schema_version(payload)
        project = self._require_text(payload, "project")
        target_branch = self._require_text(payload, "target_branch")
        base_sha = self._require_git_sha(payload, "base_sha")
        head_sha = self._require_git_sha(payload, "head_sha")
        mr_iid = self._optional_text(payload, "mr_iid")
        mr_url = self._optional_text(payload, "mr_url")
        return {
            "project": project,
            "mr_iid": mr_iid,
            "mr_url": mr_url,
            "target_branch": target_branch,
            "base_sha": base_sha,
            "head_sha": head_sha,
        }
    def _insert_cycle(self, normalized: Mapping[str, Optional[str]]) -> str:
        """Persists one already validated Cycle while its caller owns mutation ordering."""

        cycle_id = str(uuid.uuid4())
        now = self._utc_now()
        with self._database() as connection:
            connection.execute(
                """
                INSERT INTO coverage_cycles (
                    id, schema_version, project, mr_iid, mr_url, target_branch,
                    base_sha, head_sha, status, created_at, updated_at
                ) VALUES (?, 1, ?, ?, ?, ?, ?, ?, 'active', ?, ?)
                """,
                (
                    cycle_id,
                    normalized["project"],
                    normalized["mr_iid"],
                    normalized["mr_url"],
                    normalized["target_branch"],
                    normalized["base_sha"],
                    normalized["head_sha"],
                    now,
                    now,
                ),
            )
        return cycle_id
    def get_cycle(self, cycle_id: str) -> Dict[str, object]:
        """Returns one cycle and its current Build/Run counts."""

        cycle_id = self._require_uuid_value(cycle_id, "cycle_id")
        with self._database() as connection:
            row = connection.execute(
                """
                SELECT c.*,
                       (SELECT COUNT(*) FROM builds b WHERE b.cycle_id = c.id) AS build_count,
                       (SELECT COUNT(*) FROM runs r WHERE r.cycle_id = c.id) AS run_count
                FROM coverage_cycles c WHERE c.id = ?
                """,
                (cycle_id,),
            ).fetchone()
        if row is None:
            raise CoverageStoreError("cycle_not_found", "cycle_id is not registered")
        result = dict(row)
        definition = self.project_definition(result["project"])
        result["project_display_name"] = definition.display_name
        result["gitlab_host"] = definition.gitlab.host
        result["gitlab_project_path"] = definition.gitlab.project_path
        return result
    def list_cycles(self) -> List[Dict[str, object]]:
        """Returns cycles newest first with stable summary fields."""

        with self._database() as connection:
            rows = connection.execute(
                """
                SELECT c.*,
                       (SELECT COUNT(*) FROM builds b WHERE b.cycle_id = c.id) AS build_count,
                       (SELECT COUNT(*) FROM runs r WHERE r.cycle_id = c.id) AS run_count
                FROM coverage_cycles c ORDER BY c.updated_at DESC, c.id DESC
                """
            ).fetchall()
        results = []
        for row in rows:
            item = dict(row)
            definition = self.project_definition(item["project"])
            item["project_display_name"] = definition.display_name
            item["gitlab_host"] = definition.gitlab.host
            item["gitlab_project_path"] = definition.gitlab.project_path
            results.append(item)
        return results
    def update_cycle_head(self, cycle_id: str, payload: Mapping[str, object]) -> Dict[str, object]:
        """Updates only head_sha; base_sha and the cycle identity remain immutable."""

        cycle_id = self._require_uuid_value(cycle_id, "cycle_id")
        self._reject_unknown_fields(payload, {"schema_version", "head_sha"})
        self._require_schema_version(payload)
        head_sha = self._require_git_sha(payload, "head_sha")
        with self._mutation_lock, self._database() as connection:
            cursor = connection.execute(
                "UPDATE coverage_cycles SET head_sha = ?, updated_at = ? WHERE id = ?",
                (head_sha, self._utc_now(), cycle_id),
            )
            if cursor.rowcount != 1:
                raise CoverageStoreError("cycle_not_found", "cycle_id is not registered")
        return self.get_cycle(cycle_id)
