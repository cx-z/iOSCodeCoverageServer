"""Short SQLite transactions for platform users and HttpOnly web sessions."""

import hashlib
import secrets
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Dict, Iterator, List, Optional

from ioscoverage.security.passwords import (
    CredentialValidationError,
    hash_password,
    normalize_username,
    verify_password,
)


SESSION_LIFETIME = timedelta(days=7)
_DUMMY_SALT = b"ioscoverage-web!"
_DUMMY_DIGEST = bytes(32)


class AccountStoreError(Exception):
    """Carries one stable account error safe for API responses and logs."""

    def __init__(self, code: str, message: str):
        """Creates an account-domain error without storing input secrets."""

        super().__init__(message)
        self.code = code
        self.message = message


class AccountStore:
    """Owns user and session persistence through request-confined connections."""

    def __init__(
        self,
        database_path: Path,
        clock: Optional[Callable[[], datetime]] = None,
        token_factory: Optional[Callable[[], str]] = None,
    ):
        """Binds the store to an initialized v4 database and removes expired sessions."""

        self.database_path = database_path.resolve()
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._token_factory = token_factory or (lambda: secrets.token_urlsafe(32))
        if not self.database_path.is_file():
            raise AccountStoreError("database_not_found", "coverage SQLite database is missing")
        try:
            with self._database() as connection:
                version = int(connection.execute("PRAGMA user_version").fetchone()[0])
                account_table = connection.execute(
                    "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'platform_users'"
                ).fetchone()
            if version != 4 or account_table is None:
                raise AccountStoreError(
                    "database_migration_required", "coverage database must be migrated to v4 first"
                )
            self.cleanup_expired_sessions()
        except sqlite3.DatabaseError as error:
            raise AccountStoreError("database_unavailable", "coverage database is unavailable") from error

    def create_user(self, username: object, password: object, role: str) -> Dict[str, object]:
        """Creates one active developer or admin with a fresh independent password salt."""

        if role not in ("developer", "admin"):
            raise AccountStoreError("invalid_role", "role must be developer or admin")
        try:
            display_name, username_key = normalize_username(username)
            password_hash = hash_password(password)
        except CredentialValidationError as error:
            raise AccountStoreError(error.code, error.message) from error
        user_id = str(uuid.uuid4())
        now = self._now_text()
        try:
            with self._database() as connection:
                connection.execute("BEGIN IMMEDIATE")
                connection.execute(
                    """
                    INSERT INTO platform_users (
                        id, username, username_key, password_scheme, password_salt,
                        password_digest, role, status, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, 'active', ?, ?)
                    """,
                    (
                        user_id,
                        display_name,
                        username_key,
                        password_hash.scheme,
                        password_hash.salt,
                        password_hash.digest,
                        role,
                        now,
                        now,
                    ),
                )
        except sqlite3.IntegrityError as error:
            if "username_key" in str(error) or "UNIQUE constraint" in str(error):
                raise AccountStoreError("username_conflict", "username already exists") from error
            raise AccountStoreError("storage_conflict", "account could not be created") from error
        return self.get_user(user_id)

    def authenticate(self, username: object, password: object) -> Optional[Dict[str, object]]:
        """Returns one active user for valid credentials without revealing failure cause."""

        try:
            _, username_key = normalize_username(username)
        except CredentialValidationError:
            username_key = ""
        with self._database() as connection:
            row = connection.execute(
                "SELECT * FROM platform_users WHERE username_key = ?", (username_key,)
            ).fetchone()
        if row is None:
            verify_password(password, "scrypt-v1", _DUMMY_SALT, _DUMMY_DIGEST)
            return None
        if not verify_password(
            password,
            str(row["password_scheme"]),
            bytes(row["password_salt"]),
            bytes(row["password_digest"]),
        ):
            return None
        if row["status"] != "active":
            return None
        return self._project_user(row)

    def get_user(self, user_id: str) -> Dict[str, object]:
        """Returns one safe user projection without password material."""

        with self._database() as connection:
            row = connection.execute(
                "SELECT * FROM platform_users WHERE id = ?", (user_id,)
            ).fetchone()
        if row is None:
            raise AccountStoreError("user_not_found", "platform user was not found")
        return self._project_user(row)

    def list_users(self) -> List[Dict[str, object]]:
        """Lists safe account projections in stable creation order."""

        with self._database() as connection:
            rows = connection.execute(
                "SELECT * FROM platform_users ORDER BY created_at, id"
            ).fetchall()
        return [self._project_user(row) for row in rows]

    def set_status(self, user_id: str, status: str) -> Dict[str, object]:
        """Updates account availability and invalidates every session when disabling it."""

        if status not in ("active", "disabled"):
            raise AccountStoreError("invalid_status", "status must be active or disabled")
        with self._database() as connection:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                "UPDATE platform_users SET status = ?, updated_at = ? WHERE id = ?",
                (status, self._now_text(), user_id),
            )
            if cursor.rowcount != 1:
                raise AccountStoreError("user_not_found", "platform user was not found")
            if status == "disabled":
                connection.execute("DELETE FROM web_sessions WHERE user_id = ?", (user_id,))
        return self.get_user(user_id)

    def reset_password(self, user_id: str, password: object) -> Dict[str, object]:
        """Replaces one password digest and invalidates all of the user's sessions atomically."""

        try:
            password_hash = hash_password(password)
        except CredentialValidationError as error:
            raise AccountStoreError(error.code, error.message) from error
        with self._database() as connection:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                """
                UPDATE platform_users
                SET password_scheme = ?, password_salt = ?, password_digest = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    password_hash.scheme,
                    password_hash.salt,
                    password_hash.digest,
                    self._now_text(),
                    user_id,
                ),
            )
            if cursor.rowcount != 1:
                raise AccountStoreError("user_not_found", "platform user was not found")
            connection.execute("DELETE FROM web_sessions WHERE user_id = ?", (user_id,))
        return self.get_user(user_id)

    def create_session(self, user_id: str) -> Dict[str, object]:
        """Creates a fixed-lifetime session while persisting only its SHA-256 digest."""

        token = self._token_factory()
        if not isinstance(token, str) or len(token.encode("utf-8")) < 32:
            raise AccountStoreError("session_creation_failed", "session token generation failed")
        created_at = self._now()
        expires_at = created_at + SESSION_LIFETIME
        token_sha256 = self._token_digest(token)
        with self._database() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute("DELETE FROM web_sessions WHERE expires_at <= ?", (created_at.isoformat(),))
            user = connection.execute(
                "SELECT status FROM platform_users WHERE id = ?", (user_id,)
            ).fetchone()
            if user is None:
                raise AccountStoreError("user_not_found", "platform user was not found")
            if user["status"] != "active":
                raise AccountStoreError("account_disabled", "platform user is disabled")
            connection.execute(
                "INSERT INTO web_sessions (token_sha256, user_id, created_at, expires_at) VALUES (?, ?, ?, ?)",
                (token_sha256, user_id, created_at.isoformat(), expires_at.isoformat()),
            )
        return {"token": token, "expires_at": expires_at.isoformat(), "user": self.get_user(user_id)}

    def resolve_session(self, token: object) -> Optional[Dict[str, object]]:
        """Resolves one nonexpired session and rechecks the user's current active status."""

        if not isinstance(token, str) or not token:
            return None
        token_sha256 = self._token_digest(token)
        now = self._now()
        with self._database() as connection:
            row = connection.execute(
                """
                SELECT u.*, s.expires_at
                FROM web_sessions s
                JOIN platform_users u ON u.id = s.user_id
                WHERE s.token_sha256 = ?
                """,
                (token_sha256,),
            ).fetchone()
            if row is None:
                return None
            try:
                expires_at = datetime.fromisoformat(str(row["expires_at"]))
            except ValueError:
                expires_at = now
            if expires_at <= now or row["status"] != "active":
                connection.execute(
                    "DELETE FROM web_sessions WHERE token_sha256 = ?", (token_sha256,)
                )
                return None
        return self._project_user(row)

    def logout(self, token: object) -> None:
        """Deletes only the supplied browser session and treats unknown tokens as already logged out."""

        if not isinstance(token, str) or not token:
            return
        with self._database() as connection:
            connection.execute(
                "DELETE FROM web_sessions WHERE token_sha256 = ?", (self._token_digest(token),)
            )

    def cleanup_expired_sessions(self) -> int:
        """Removes sessions whose fixed server-side expiration has passed."""

        with self._database() as connection:
            cursor = connection.execute(
                "DELETE FROM web_sessions WHERE expires_at <= ?", (self._now_text(),)
            )
        return int(cursor.rowcount)

    @contextmanager
    def _database(self) -> Iterator[sqlite3.Connection]:
        """Yields one thread-confined connection and commits or rolls back its transaction."""

        connection = sqlite3.connect(str(self.database_path), timeout=10, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 10000")
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _project_user(self, row: sqlite3.Row) -> Dict[str, object]:
        """Projects one user row while excluding password columns and normalized login keys."""

        return {
            "id": str(row["id"]),
            "username": str(row["username"]),
            "role": str(row["role"]),
            "status": str(row["status"]),
            "created_at": str(row["created_at"]),
            "updated_at": str(row["updated_at"]),
        }

    def _now(self) -> datetime:
        """Returns a timezone-aware UTC clock value from the injected source."""

        value = self._clock()
        if value.tzinfo is None:
            raise AccountStoreError("invalid_clock", "account clock must include a timezone")
        return value.astimezone(timezone.utc)

    def _now_text(self) -> str:
        """Serializes the current UTC time for stable lexical ordering."""

        return self._now().isoformat()

    def _token_digest(self, token: str) -> str:
        """Returns the persisted one-way identity for a raw browser session token."""

        return hashlib.sha256(token.encode("utf-8")).hexdigest()
