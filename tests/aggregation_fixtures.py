"""Fictional indexed provenance fixtures shared by isolated protocol tests."""
import uuid


def indexed_metadata(raw):
    """Builds two sorted cross-launch sources without creating server source rows."""
    result = dict(raw, profile_format="indexed", trigger_reason="cross_launch_merge")
    result["aggregation"] = {"schema_version": 1, "merger_version": "llvm-20.1.8/cck-1", "sources": sorted([
        {"run_id": str(uuid.uuid4()), "launch_id": str(uuid.uuid4()), "process_id": 123,
         "captured_at": "2026-01-01T00:00:00Z", "size": 344, "sha256": "a" * 64,
         "metadata_sha256": "b" * 64, "capture_sequence": index + 1}
        for index in range(2)], key=lambda source: source["run_id"])}
    return result
