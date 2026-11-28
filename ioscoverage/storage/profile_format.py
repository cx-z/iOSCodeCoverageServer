"""Fixed profile locations; metadata can never supply a filesystem path."""

from typing import Mapping
from ioscoverage.storage.common import CoverageStoreError


def profile_format(metadata: Mapping[str, object]) -> str:
    """Resolves the optional schema-1 extension without changing legacy identity."""
    value = metadata.get("profile_format", "raw")
    if value not in ("raw", "indexed"):
        raise CoverageStoreError("invalid_metadata", "unsupported profile_format")
    return str(value)


def payload_name(metadata: Mapping[str, object]) -> str:
    """Returns only a server-owned constant for the validated format."""
    return "payload.profdata" if profile_format(metadata) == "indexed" else "payload.profraw"


def profile_extension(metadata: Mapping[str, object]) -> str:
    """Keeps the HTTP filename and durable payload format in agreement."""
    return ".profdata" if profile_format(metadata) == "indexed" else ".profraw"
