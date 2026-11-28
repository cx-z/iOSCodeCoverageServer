"""Bounded, declarative provenance validation, never deletion authorization."""

from typing import Mapping
from ioscoverage.storage.common import CoverageStoreError, SHA256_PATTERN
from ioscoverage.storage.profile_format import profile_format

MAX_AGGREGATION_SOURCES = 64
MAX_AGGREGATION_METADATA_BYTES = 64 * 1024


def validate_aggregation(store, metadata: Mapping[str, object]) -> None:
    """Rejects ambiguous sources while retaining the exact submitted metadata identity."""
    def reject():
        raise CoverageStoreError("invalid_metadata", "invalid indexed aggregation")

    if profile_format(metadata) == "raw":
        if "aggregation" in metadata:
            reject()
        return
    aggregation = metadata.get("aggregation")
    if not isinstance(aggregation, dict) or set(aggregation) != {"schema_version", "merger_version", "sources"}:
        reject()
    if type(aggregation["schema_version"]) is not int or aggregation["schema_version"] != 1:
        reject()
    merger = aggregation["merger_version"]
    if not isinstance(merger, str) or not merger.strip() or len(merger.encode("utf-8")) > 128:
        reject()
    sources = aggregation["sources"]
    if not isinstance(sources, list) or not 2 <= len(sources) <= MAX_AGGREGATION_SOURCES:
        reject()
    if len(store._canonical_json_bytes(metadata)) > MAX_AGGREGATION_METADATA_BYTES:
        reject()
    required = {"run_id", "launch_id", "process_id", "captured_at", "size", "sha256", "metadata_sha256"}
    ids = []
    launches = set()
    for source in sources:
        if not isinstance(source, dict) or not required <= set(source) or set(source) - required - {"capture_sequence"}:
            reject()
        for field in ("run_id", "launch_id"):
            if store._require_uuid_field(source, field) != source[field]:
                reject()
        if source["run_id"] == store._require_uuid_field(metadata, "run_id"):
            reject()
        for field in ("size", "process_id", "capture_sequence"):
            if field in source and (type(source[field]) is not int or source[field] <= 0 or source[field] > 2**63 - 1):
                reject()
        for field in ("sha256", "metadata_sha256"):
            if not isinstance(source[field], str) or not SHA256_PATTERN.fullmatch(source[field]):
                reject()
        store._require_timestamp(store._require_text(source, "captured_at"), "captured_at")
        ids.append(source["run_id"])
        launches.add(source["launch_id"])
    if ids != sorted(set(ids)) or len(launches) < 2 or metadata.get("trigger_reason") != "cross_launch_merge":
        reject()
