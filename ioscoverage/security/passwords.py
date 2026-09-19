"""Username normalization and versioned password hashing for platform accounts."""

import hashlib
import ctypes
import re
import secrets
import struct
import sys
from array import array
from dataclasses import dataclass
from typing import Tuple


PASSWORD_SCHEME = "scrypt-v1"
PASSWORD_SALT_BYTES = 16
PASSWORD_DIGEST_BYTES = 32
PASSWORD_MAX_UTF8_BYTES = 256
USERNAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{2,31}$")


class CredentialValidationError(ValueError):
    """Carries one stable validation code without retaining credential material."""

    def __init__(self, code: str, message: str):
        """Creates a safe validation error containing no supplied value."""

        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class PasswordHash:
    """Contains the versioned, independently salted password representation."""

    scheme: str
    salt: bytes
    digest: bytes


def normalize_username(value: object) -> Tuple[str, str]:
    """Validates one display username and returns it with its case-insensitive key."""

    if not isinstance(value, str) or not USERNAME_PATTERN.fullmatch(value):
        raise CredentialValidationError(
            "invalid_username",
            "username must be 3-32 ASCII letters, digits, dots, underscores, or hyphens",
        )
    return value, value.lower()


def validate_password(value: object) -> str:
    """Accepts a bounded Unicode password without imposing composition rules."""

    if not isinstance(value, str) or not 8 <= len(value) <= 128:
        raise CredentialValidationError(
            "invalid_password", "password must contain 8-128 characters"
        )
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError as error:
        raise CredentialValidationError(
            "invalid_password", "password must be valid UTF-8 text"
        ) from error
    if len(encoded) > PASSWORD_MAX_UTF8_BYTES:
        raise CredentialValidationError(
            "invalid_password", "password UTF-8 representation is too large"
        )
    return value


def hash_password(value: object) -> PasswordHash:
    """Hashes one validated password with a fresh salt and fixed scrypt-v1 parameters."""

    password = validate_password(value)
    salt = secrets.token_bytes(PASSWORD_SALT_BYTES)
    return PasswordHash(PASSWORD_SCHEME, salt, _derive(password, salt))


def verify_password(value: object, scheme: str, salt: bytes, digest: bytes) -> bool:
    """Compares one candidate against a supported stored digest in constant time."""

    if scheme != PASSWORD_SCHEME:
        return False
    try:
        password = validate_password(value)
    except CredentialValidationError:
        return False
    if not isinstance(salt, bytes) or len(salt) != PASSWORD_SALT_BYTES:
        return False
    if not isinstance(digest, bytes) or len(digest) != PASSWORD_DIGEST_BYTES:
        return False
    return secrets.compare_digest(_derive(password, salt), digest)


def _derive(password: str, salt: bytes) -> bytes:
    """Derives the fixed scrypt-v1 digest with an explicit OpenSSL memory allowance."""

    implementation = getattr(hashlib, "scrypt", None)
    if implementation is not None:
        return implementation(
            password.encode("utf-8"),
            salt=salt,
            n=16384,
            r=8,
            p=1,
            dklen=PASSWORD_DIGEST_BYTES,
            maxmem=64 * 1024 * 1024,
        )
    encoded = password.encode("utf-8")
    openssl_result = _openssl_scrypt(encoded, salt, 16384, 8, 1, PASSWORD_DIGEST_BYTES)
    if openssl_result is not None:
        return openssl_result
    return _stdlib_scrypt(encoded, salt, 16384, 8, 1, PASSWORD_DIGEST_BYTES)


def _openssl_scrypt(
    password: bytes, salt: bytes, cost: int, block_size: int, parallelism: int, length: int
):
    """Uses an installed OpenSSL 3 scrypt primitive when Apple's Python omits hashlib.scrypt."""

    candidates = (
        "/opt/homebrew/opt/openssl@3/lib/libcrypto.3.dylib",
        "/usr/local/opt/openssl@3/lib/libcrypto.3.dylib",
    )
    library = None
    for candidate in candidates:
        try:
            library = ctypes.CDLL(candidate)
            function = library.EVP_PBE_scrypt
            break
        except (OSError, AttributeError):
            library = None
    if library is None:
        return None
    function.argtypes = (
        ctypes.c_char_p,
        ctypes.c_size_t,
        ctypes.c_char_p,
        ctypes.c_size_t,
        ctypes.c_uint64,
        ctypes.c_uint64,
        ctypes.c_uint64,
        ctypes.c_uint64,
        ctypes.c_void_p,
        ctypes.c_size_t,
    )
    function.restype = ctypes.c_int
    output = ctypes.create_string_buffer(length)
    status = function(
        password,
        len(password),
        salt,
        len(salt),
        cost,
        block_size,
        parallelism,
        64 * 1024 * 1024,
        output,
        length,
    )
    if status != 1:
        raise RuntimeError("OpenSSL scrypt derivation failed")
    return output.raw


def _stdlib_scrypt(
    password: bytes, salt: bytes, cost: int, block_size: int, parallelism: int, length: int
) -> bytes:
    """Implements RFC 7914 with stdlib primitives when this Python omits hashlib.scrypt."""

    block_bytes = 128 * block_size
    initial = hashlib.pbkdf2_hmac(
        "sha256", password, salt, 1, dklen=parallelism * block_bytes
    )
    mixed = bytearray()
    for index in range(parallelism):
        start = index * block_bytes
        mixed.extend(_romix(initial[start : start + block_bytes], cost, block_size))
    return hashlib.pbkdf2_hmac("sha256", password, bytes(mixed), 1, dklen=length)


def _romix(block: bytes, cost: int, block_size: int) -> bytes:
    """Runs the memory-hard RFC 7914 ROMix step for one parallel block."""

    word_count = 32 * block_size
    words = list(struct.unpack("<{}I".format(word_count), block))
    memory = array("I", [0]) * (cost * word_count)
    if sys.byteorder != "little":
        raise RuntimeError("scrypt fallback requires little-endian storage")
    for index in range(cost):
        start = index * word_count
        memory[start : start + word_count] = array("I", words)
        words = _block_mix(words, block_size)
    for _ in range(cost):
        selected = words[(2 * block_size - 1) * 16] & (cost - 1)
        start = selected * word_count
        words = [value ^ memory[start + offset] for offset, value in enumerate(words)]
        words = _block_mix(words, block_size)
    return struct.pack("<{}I".format(word_count), *words)


def _block_mix(words: list, block_size: int) -> list:
    """Applies Salsa20/8 BlockMix and the RFC even/odd output permutation."""

    state = words[(2 * block_size - 1) * 16 : 2 * block_size * 16]
    output = []
    for index in range(2 * block_size):
        start = index * 16
        state = _salsa20_8([left ^ right for left, right in zip(state, words[start : start + 16])])
        output.append(state)
    return [value for part in output[0::2] + output[1::2] for value in part]


def _salsa20_8(state: list) -> list:
    """Computes the Salsa20/8 core over sixteen little-endian 32-bit words."""

    values = list(state)
    for _ in range(4):
        values[4] ^= _rotate(values[0] + values[12], 7)
        values[8] ^= _rotate(values[4] + values[0], 9)
        values[12] ^= _rotate(values[8] + values[4], 13)
        values[0] ^= _rotate(values[12] + values[8], 18)
        values[9] ^= _rotate(values[5] + values[1], 7)
        values[13] ^= _rotate(values[9] + values[5], 9)
        values[1] ^= _rotate(values[13] + values[9], 13)
        values[5] ^= _rotate(values[1] + values[13], 18)
        values[14] ^= _rotate(values[10] + values[6], 7)
        values[2] ^= _rotate(values[14] + values[10], 9)
        values[6] ^= _rotate(values[2] + values[14], 13)
        values[10] ^= _rotate(values[6] + values[2], 18)
        values[3] ^= _rotate(values[15] + values[11], 7)
        values[7] ^= _rotate(values[3] + values[15], 9)
        values[11] ^= _rotate(values[7] + values[3], 13)
        values[15] ^= _rotate(values[11] + values[7], 18)
        values[1] ^= _rotate(values[0] + values[3], 7)
        values[2] ^= _rotate(values[1] + values[0], 9)
        values[3] ^= _rotate(values[2] + values[1], 13)
        values[0] ^= _rotate(values[3] + values[2], 18)
        values[6] ^= _rotate(values[5] + values[4], 7)
        values[7] ^= _rotate(values[6] + values[5], 9)
        values[4] ^= _rotate(values[7] + values[6], 13)
        values[5] ^= _rotate(values[4] + values[7], 18)
        values[11] ^= _rotate(values[10] + values[9], 7)
        values[8] ^= _rotate(values[11] + values[10], 9)
        values[9] ^= _rotate(values[8] + values[11], 13)
        values[10] ^= _rotate(values[9] + values[8], 18)
        values[12] ^= _rotate(values[15] + values[14], 7)
        values[13] ^= _rotate(values[12] + values[15], 9)
        values[14] ^= _rotate(values[13] + values[12], 13)
        values[15] ^= _rotate(values[14] + values[13], 18)
        values = [value & 0xFFFFFFFF for value in values]
    return [(value + original) & 0xFFFFFFFF for value, original in zip(values, state)]


def _rotate(value: int, bits: int) -> int:
    """Rotates one unsigned 32-bit value left after modular addition."""

    value &= 0xFFFFFFFF
    return ((value << bits) | (value >> (32 - bits))) & 0xFFFFFFFF
