"""Shared storage constants and stable domain errors."""

import re


SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
GIT_SHA_PATTERN = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
IDENTIFIER_MAX_LENGTH = 255
REQUIRED_ARTIFACT_FIELDS = ("main_executable", "dsym")
OPTIONAL_ARTIFACT_FIELDS = ("debug_dylib",)
ARTIFACT_FIELDS = REQUIRED_ARTIFACT_FIELDS + OPTIONAL_ARTIFACT_FIELDS
ARTIFACT_STORAGE_NAMES = {
    "main_executable": "main_executable.bin",
    "debug_dylib": "debug_dylib.bin",
    "dsym": "dsym.zip",
}


class CoverageStoreError(Exception):
    """Carries a stable domain error code and safe client-facing explanation."""

    def __init__(self, code: str, message: str):
        """Creates one error without coupling persistence to HTTP response types."""

        super().__init__(message)
        self.code = code
        self.message = message
