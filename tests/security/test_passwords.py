#!/usr/bin/env python3
"""Focused tests for username and password security primitives."""

import unittest

from ioscoverage.security.passwords import (
    CredentialValidationError,
    _stdlib_scrypt,
    hash_password,
    normalize_username,
    validate_password,
    verify_password,
)


class PasswordSecurityTests(unittest.TestCase):
    """Locks the local account validation and scrypt-v1 contract."""

    def test_username_rules_and_case_insensitive_key(self) -> None:
        """Accepts the documented ASCII form and derives a lowercase unique key."""

        self.assertEqual(normalize_username("Dev.User-1"), ("Dev.User-1", "dev.user-1"))
        for value in ("ab", "a" * 33, "_abc", "user name", "用户123"):
            with self.subTest(value=value):
                with self.assertRaises(CredentialValidationError) as caught:
                    normalize_username(value)
                self.assertEqual(caught.exception.code, "invalid_username")

    def test_password_boundaries_and_utf8_limit(self) -> None:
        """Accepts bounded Unicode while rejecting short, long, and oversized encodings."""

        self.assertEqual(validate_password("12345678"), "12345678")
        self.assertEqual(validate_password("密" * 64), "密" * 64)
        for value in ("short", "x" * 129, "😀" * 128):
            with self.subTest(length=len(value)):
                with self.assertRaises(CredentialValidationError) as caught:
                    validate_password(value)
                self.assertEqual(caught.exception.code, "invalid_password")

    def test_scrypt_uses_independent_salts_and_constant_time_verification(self) -> None:
        """Produces distinct stored values and validates only the exact password."""

        first = hash_password("correct horse")
        second = hash_password("correct horse")
        self.assertEqual(first.scheme, "scrypt-v1")
        self.assertNotEqual(first.salt, second.salt)
        self.assertNotEqual(first.digest, second.digest)
        self.assertTrue(verify_password("correct horse", first.scheme, first.salt, first.digest))
        self.assertFalse(verify_password("wrong password", first.scheme, first.salt, first.digest))

    def test_stdlib_fallback_matches_rfc_7914_vector(self) -> None:
        """Keeps the no-OpenSSL fallback byte-compatible with the scrypt standard."""

        expected = bytes.fromhex(
            "77d6576238657b203b19ca42c18a0497f16b4844e3074ae8dfdffa3fede21442"
            "fcd0069ded0948f8326a753a0fc81f17e8d3e0fb2e0d3628cf35e20c38d18906"
        )
        self.assertEqual(_stdlib_scrypt(b"", b"", 16, 1, 1, 64), expected)


if __name__ == "__main__":
    unittest.main()
