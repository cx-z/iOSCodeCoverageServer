#!/usr/bin/env python3
"""Tests for persistent and explicitly rotatable server credentials."""

import stat
import tempfile
import unittest
from pathlib import Path

from ioscoverage.storage.tokens import TokenStore


class TokenStoreTests(unittest.TestCase):
    """Exercises restart persistence, replacement, and local file permissions."""

    def test_token_survives_restart_and_uses_owner_only_permissions(self) -> None:
        """Confirms two store instances read the same persisted credential."""

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state" / "upload_token"
            first = TokenStore(path).load_or_create()
            second = TokenStore(path).load_or_create()
            self.assertEqual(first, second)
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)

    def test_rotation_replaces_persisted_token(self) -> None:
        """Confirms explicit rotation changes the current and subsequent token."""

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "upload_token"
            store = TokenStore(path)
            original = store.load_or_create()
            rotated = store.rotate()
            self.assertNotEqual(original, rotated)
            self.assertEqual(TokenStore(path).load_or_create(), rotated)

    def test_explicit_token_is_persisted(self) -> None:
        """Confirms the compatibility flag establishes a stable operator-chosen token."""

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "upload_token"
            explicit = "operator-selected-token-1234"
            self.assertEqual(TokenStore(path).load_or_create(explicit), explicit)
            self.assertEqual(TokenStore(path).load_or_create(), explicit)

    def test_independent_run_upload_token_survives_restart_with_mode_0600(self) -> None:
        """Keeps the iOS credential separate from the operator credential across restarts."""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            operator_token = TokenStore(root / "upload_token").load_or_create()
            run_path = root / "run_upload_token"
            first_run_token = TokenStore(run_path).load_or_create()
            second_run_token = TokenStore(run_path).load_or_create()
            self.assertNotEqual(operator_token, first_run_token)
            self.assertEqual(first_run_token, second_run_token)
            self.assertEqual(stat.S_IMODE(run_path.stat().st_mode), 0o600)

    def test_packaging_token_is_persistent_owner_only_and_distinct(self) -> None:
        """Keeps the build-machine credential separate with owner-only permissions."""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            operator = TokenStore(root / "operator_token").load_or_create()
            packaging_path = root / "packaging_token"
            packaging = TokenStore(packaging_path).load_or_create()
            run_upload = TokenStore(root / "run_upload_token").load_or_create()
            self.assertEqual(TokenStore(packaging_path).load_or_create(), packaging)
            self.assertEqual(len({operator, packaging, run_upload}), 3)
            self.assertEqual(stat.S_IMODE(packaging_path.stat().st_mode), 0o600)


if __name__ == "__main__":
    unittest.main()
