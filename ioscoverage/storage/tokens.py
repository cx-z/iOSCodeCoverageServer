#!/usr/bin/env python3
"""Persistent Bearer-token storage for the local iOS coverage server."""

import os
import secrets
import tempfile
from pathlib import Path
from typing import Optional


class TokenStore:
    """Owns creation, loading, and explicit rotation of one local server token."""

    def __init__(self, path: Path):
        """Binds token operations to one resolved file path."""

        self.path = path.resolve()

    def load_or_create(self, explicit_token: Optional[str] = None) -> str:
        """Returns the persisted token, atomically creating or replacing it when requested."""

        if explicit_token is not None:
            token = self._validate(explicit_token)
            self._persist(token)
            return token
        try:
            token = self.path.read_text(encoding="utf-8").strip()
        except FileNotFoundError:
            token = secrets.token_urlsafe(32)
            self._persist(token)
            return token
        os.chmod(str(self.path), 0o600)
        return self._validate(token)

    def rotate(self) -> str:
        """Generates and atomically persists a new token as an explicit operator action."""

        token = secrets.token_urlsafe(32)
        self._persist(token)
        return token

    def _validate(self, token: str) -> str:
        """Rejects blank, whitespace-bearing, or implausibly short credentials."""

        if len(token) < 16 or token.strip() != token or any(character.isspace() for character in token):
            raise ValueError("token must be at least 16 non-whitespace characters")
        return token

    def _persist(self, token: str) -> None:
        """Writes a token with owner-only permissions before atomically exposing it."""

        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary_path = ""
        try:
            descriptor, temporary_path = tempfile.mkstemp(
                dir=str(self.path.parent), prefix=".upload-token-"
            )
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as temporary_file:
                temporary_file.write(token + "\n")
                temporary_file.flush()
                os.fsync(temporary_file.fileno())
            os.replace(temporary_path, str(self.path))
            os.chmod(str(self.path), 0o600)
        finally:
            if temporary_path and os.path.exists(temporary_path):
                os.unlink(temporary_path)
