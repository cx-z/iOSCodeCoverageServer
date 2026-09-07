#!/usr/bin/env python3
"""Persistent generation-safe state for background Cycle analysis."""

import json
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Iterator, List, Mapping, Optional


ANALYSIS_STATE_SCHEMA_VERSION = 1
DATABASE_USER_VERSION = 3
ANALYSIS_STATUSES = ("idle", "queued", "running", "succeeded", "failed")
ANALYSIS_TRIGGERS = (
    "run_uploaded",
    "pending_runs_promoted",
    "manual_retry",
    "service_restart_recovery",
)


class AnalysisStateStoreError(Exception):
    """Carries one stable state-store error safe for HTTP or logs."""

    def __init__(self, code: str, message: str):
        """Creates a bounded persistence error."""

        super().__init__(message)
        self.code = code
        self.message = message


class AnalysisStateStore:
    """Owns short SQLite transactions for one row per Cycle scheduling state."""

    def __init__(self, database_path: Path):
        """Applies the additive analysis-state migration without rewriting existing tables."""

        self.database_path = database_path.resolve()
        if not self.database_path.is_file():
            raise AnalysisStateStoreError(
                "database_not_found", "coverage SQLite database is missing"
            )
        self._initialize_schema()

    def request_analysis(
        self, cycle_id: str, trigger: str, latest_run_id: Optional[str] = None
    ) -> Dict[str, object]:
        """Increments the requested generation and queues or coalesces one Cycle."""

        cycle_id = self._normalize_uuid(cycle_id, "cycle_id")
        if trigger not in ANALYSIS_TRIGGERS or trigger == "service_restart_recovery":
            raise AnalysisStateStoreError("invalid_trigger", "analysis trigger is invalid")
        if latest_run_id is not None:
            latest_run_id = self._normalize_uuid(latest_run_id, "latest_run_id")
        now = self._utc_now()
        with self._database() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._require_cycle(connection, cycle_id)
            row = connection.execute(
                "SELECT * FROM cycle_analysis_states WHERE cycle_id = ?", (cycle_id,)
            ).fetchone()
            if row is None:
                connection.execute(
                    """
                    INSERT INTO cycle_analysis_states (
                        cycle_id, schema_version, requested_generation,
                        completed_generation, running_generation, status,
                        requested_at, started_at, completed_at, attempt_count,
                        recovery_count, last_trigger, latest_run_id
                    ) VALUES (?, 1, 1, 0, NULL, 'queued', ?, NULL, NULL, 0, 0, ?, ?)
                    """,
                    (cycle_id, now, trigger, latest_run_id),
                )
            else:
                status = "running" if row["status"] == "running" else "queued"
                connection.execute(
                    """
                    UPDATE cycle_analysis_states
                    SET requested_generation = requested_generation + 1,
                        status = ?, requested_at = ?, last_trigger = ?, latest_run_id = ?,
                        error_stage = NULL, error_code = NULL, error_message = NULL
                    WHERE cycle_id = ?
                    """,
                    (status, now, trigger, latest_run_id, cycle_id),
                )
            connection.commit()
        return self.get_state(cycle_id)

    def get_state(self, cycle_id: str) -> Dict[str, object]:
        """Returns persisted state or a read-only synthesized idle state."""

        cycle_id = self._normalize_uuid(cycle_id, "cycle_id")
        with self._database() as connection:
            self._require_cycle(connection, cycle_id)
            row = connection.execute(
                "SELECT * FROM cycle_analysis_states WHERE cycle_id = ?", (cycle_id,)
            ).fetchone()
        if row is None:
            return self._idle_state(cycle_id)
        return self._row_state(row)

    def recover_interrupted(self) -> List[str]:
        """Moves process-interrupted running rows back to queued exactly once per startup."""

        now = self._utc_now()
        with self._database() as connection:
            connection.execute("BEGIN IMMEDIATE")
            rows = connection.execute(
                "SELECT cycle_id FROM cycle_analysis_states WHERE status = 'running' ORDER BY cycle_id"
            ).fetchall()
            cycle_ids = [str(row["cycle_id"]) for row in rows]
            if cycle_ids:
                connection.execute(
                    """
                    UPDATE cycle_analysis_states
                    SET status = 'queued', running_generation = NULL,
                        recovery_count = recovery_count + 1,
                        last_trigger = 'service_restart_recovery', requested_at = ?
                    WHERE status = 'running'
                    """,
                    (now,),
                )
            connection.commit()
        return cycle_ids

    def queued_cycles(self) -> List[str]:
        """Lists persisted queued Cycles in stable request order."""

        with self._database() as connection:
            rows = connection.execute(
                """
                SELECT cycle_id FROM cycle_analysis_states
                WHERE status = 'queued'
                ORDER BY requested_at, cycle_id
                """
            ).fetchall()
        return [str(row["cycle_id"]) for row in rows]

    def claim(self, cycle_id: str) -> Optional[int]:
        """Atomically assigns the latest requested generation to the single worker."""

        cycle_id = self._normalize_uuid(cycle_id, "cycle_id")
        now = self._utc_now()
        with self._database() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM cycle_analysis_states WHERE cycle_id = ?", (cycle_id,)
            ).fetchone()
            if row is None or row["status"] != "queued":
                connection.commit()
                return None
            generation = int(row["requested_generation"])
            connection.execute(
                """
                UPDATE cycle_analysis_states
                SET status = 'running', running_generation = ?, started_at = ?,
                    completed_at = NULL, attempt_count = attempt_count + 1
                WHERE cycle_id = ? AND status = 'queued'
                """,
                (generation, now, cycle_id),
            )
            connection.commit()
            return generation

    def complete_success(
        self, cycle_id: str, generation: int, result: Mapping[str, object]
    ) -> Dict[str, object]:
        """Commits one successful generation without overwriting a newer request."""

        cycle_id = self._normalize_uuid(cycle_id, "cycle_id")
        self._require_positive_generation(generation)
        required = {
            "orchestration_input_sha256",
            "cycle_input_set_sha256",
            "current_build_id",
            "output_directory",
            "summary",
        }
        if set(result) != required or not isinstance(result["summary"], dict):
            raise AnalysisStateStoreError("invalid_analysis_result", "analysis result is invalid")
        now = self._utc_now()
        summary_json = self._canonical_json(result["summary"])
        with self._database() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = self._running_row(connection, cycle_id, generation)
            if row is None:
                connection.commit()
                return self.get_state(cycle_id)
            next_status = (
                "queued" if int(row["requested_generation"]) > generation else "succeeded"
            )
            connection.execute(
                """
                UPDATE cycle_analysis_states
                SET completed_generation = ?, running_generation = NULL, status = ?,
                    completed_at = ?, orchestration_input_sha256 = ?,
                    cycle_input_set_sha256 = ?, current_build_id = ?, output_directory = ?,
                    latest_success_generation = ?,
                    latest_success_orchestration_sha256 = ?,
                    latest_success_cycle_input_sha256 = ?,
                    latest_success_current_build_id = ?,
                    latest_success_output_directory = ?,
                    latest_success_summary_json = ?,
                    error_stage = NULL, error_code = NULL, error_message = NULL
                WHERE cycle_id = ? AND status = 'running' AND running_generation = ?
                """,
                (
                    generation,
                    next_status,
                    now,
                    result["orchestration_input_sha256"],
                    result["cycle_input_set_sha256"],
                    result["current_build_id"],
                    result["output_directory"],
                    generation,
                    result["orchestration_input_sha256"],
                    result["cycle_input_set_sha256"],
                    result["current_build_id"],
                    result["output_directory"],
                    summary_json,
                    cycle_id,
                    generation,
                ),
            )
            connection.commit()
        return self.get_state(cycle_id)

    def complete_failure(
        self,
        cycle_id: str,
        generation: int,
        error_stage: str,
        error_code: str,
        error_message: str,
    ) -> Dict[str, object]:
        """Records one bounded failure while preserving the latest successful report."""

        cycle_id = self._normalize_uuid(cycle_id, "cycle_id")
        self._require_positive_generation(generation)
        stage = self._bounded_text(error_stage, "error_stage")
        code = self._bounded_text(error_code, "error_code")
        message = self._bounded_text(error_message, "error_message", 1024)
        now = self._utc_now()
        with self._database() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = self._running_row(connection, cycle_id, generation)
            if row is None:
                connection.commit()
                return self.get_state(cycle_id)
            next_status = "queued" if int(row["requested_generation"]) > generation else "failed"
            connection.execute(
                """
                UPDATE cycle_analysis_states
                SET completed_generation = ?, running_generation = NULL, status = ?,
                    completed_at = ?, error_stage = ?, error_code = ?, error_message = ?
                WHERE cycle_id = ? AND status = 'running' AND running_generation = ?
                """,
                (generation, next_status, now, stage, code, message, cycle_id, generation),
            )
            connection.commit()
        return self.get_state(cycle_id)

    def _initialize_schema(self) -> None:
        """Adds the analysis task table and advances only the additive database version."""

        with self._database() as connection:
            version = int(connection.execute("PRAGMA user_version").fetchone()[0])
            if version > DATABASE_USER_VERSION:
                raise AnalysisStateStoreError(
                    "unsupported_database_schema", "coverage database is newer than this server"
                )
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS cycle_analysis_states (
                    cycle_id TEXT PRIMARY KEY REFERENCES coverage_cycles(id),
                    schema_version INTEGER NOT NULL CHECK(schema_version = 1),
                    requested_generation INTEGER NOT NULL DEFAULT 0 CHECK(requested_generation >= 0),
                    completed_generation INTEGER NOT NULL DEFAULT 0 CHECK(completed_generation >= 0),
                    running_generation INTEGER,
                    status TEXT NOT NULL CHECK(status IN ('idle','queued','running','succeeded','failed')),
                    requested_at TEXT,
                    started_at TEXT,
                    completed_at TEXT,
                    attempt_count INTEGER NOT NULL DEFAULT 0 CHECK(attempt_count >= 0),
                    recovery_count INTEGER NOT NULL DEFAULT 0 CHECK(recovery_count >= 0),
                    last_trigger TEXT,
                    latest_run_id TEXT,
                    orchestration_input_sha256 TEXT,
                    cycle_input_set_sha256 TEXT,
                    current_build_id TEXT,
                    output_directory TEXT,
                    latest_success_generation INTEGER,
                    latest_success_orchestration_sha256 TEXT,
                    latest_success_cycle_input_sha256 TEXT,
                    latest_success_current_build_id TEXT,
                    latest_success_output_directory TEXT,
                    latest_success_summary_json TEXT,
                    error_stage TEXT,
                    error_code TEXT,
                    error_message TEXT
                );
                CREATE INDEX IF NOT EXISTS cycle_analysis_states_status_index
                ON cycle_analysis_states(status, requested_at, cycle_id);
                """
            )
            if version < DATABASE_USER_VERSION:
                connection.execute("PRAGMA user_version = 3")
            connection.commit()

    def _connection(self) -> sqlite3.Connection:
        """Returns one thread-confined connection; callers never share it across threads."""

        connection = sqlite3.connect(str(self.database_path), timeout=10, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 10000")
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute("PRAGMA synchronous = FULL")
        return connection

    @contextmanager
    def _database(self) -> Iterator[sqlite3.Connection]:
        """Confines each connection to one caller/thread and always closes it."""

        connection = self._connection()
        try:
            yield connection
        except Exception:
            if connection.in_transaction:
                connection.rollback()
            raise
        finally:
            connection.close()

    def _require_cycle(self, connection: sqlite3.Connection, cycle_id: str) -> None:
        """Rejects unknown Cycle identities before creating scheduling state."""

        row = connection.execute(
            "SELECT 1 FROM coverage_cycles WHERE id = ?", (cycle_id,)
        ).fetchone()
        if row is None:
            raise AnalysisStateStoreError("cycle_not_found", "cycle_id is not registered")

    def _running_row(
        self, connection: sqlite3.Connection, cycle_id: str, generation: int
    ) -> Optional[sqlite3.Row]:
        """Returns only the exact running generation allowed to commit a result."""

        return connection.execute(
            """
            SELECT * FROM cycle_analysis_states
            WHERE cycle_id = ? AND status = 'running' AND running_generation = ?
            """,
            (cycle_id, generation),
        ).fetchone()

    def _row_state(self, row: sqlite3.Row) -> Dict[str, object]:
        """Converts one database row into an internal state document."""

        result = dict(row)
        summary = result.pop("latest_success_summary_json")
        result["latest_success_summary"] = json.loads(summary) if summary is not None else None
        return result

    def _idle_state(self, cycle_id: str) -> Dict[str, object]:
        """Returns explicit idle semantics without mutating SQLite on GET."""

        return {
            "cycle_id": cycle_id,
            "schema_version": ANALYSIS_STATE_SCHEMA_VERSION,
            "requested_generation": 0,
            "completed_generation": 0,
            "running_generation": None,
            "status": "idle",
            "requested_at": None,
            "started_at": None,
            "completed_at": None,
            "attempt_count": 0,
            "recovery_count": 0,
            "last_trigger": None,
            "latest_run_id": None,
            "orchestration_input_sha256": None,
            "cycle_input_set_sha256": None,
            "current_build_id": None,
            "output_directory": None,
            "latest_success_generation": None,
            "latest_success_orchestration_sha256": None,
            "latest_success_cycle_input_sha256": None,
            "latest_success_current_build_id": None,
            "latest_success_output_directory": None,
            "latest_success_summary": None,
            "error_stage": None,
            "error_code": None,
            "error_message": None,
        }

    def _normalize_uuid(self, value: str, field: str) -> str:
        """Accepts only canonical UUID values for stored task identities."""

        try:
            normalized = str(uuid.UUID(value))
        except (ValueError, AttributeError, TypeError) as error:
            raise AnalysisStateStoreError("invalid_uuid", field + " must be a UUID") from error
        if normalized != value:
            raise AnalysisStateStoreError("invalid_uuid", field + " must be canonical")
        return normalized

    def _require_positive_generation(self, value: int) -> None:
        """Rejects stale or invalid generation values at the transaction boundary."""

        if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
            raise AnalysisStateStoreError("invalid_generation", "generation must be positive")

    def _bounded_text(self, value: str, field: str, limit: int = 128) -> str:
        """Bounds failure evidence and rejects empty unsafe diagnostic values."""

        if not isinstance(value, str) or not value or len(value) > limit:
            raise AnalysisStateStoreError("invalid_" + field, field + " is invalid")
        return value

    def _canonical_json(self, payload: Mapping[str, object]) -> str:
        """Stores only a small deterministic summary, never the LLVM export."""

        return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)

    def _utc_now(self) -> str:
        """Returns one UTC timestamp for persisted state transitions."""

        return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
