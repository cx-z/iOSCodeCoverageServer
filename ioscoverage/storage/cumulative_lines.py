"""Versioned cumulative-line encoding over the existing mapping-evidence table.

The codec only changes representation. Callers own completion verification and
coverage decisions; decoded region dictionaries are shared within one read.
"""

import hashlib
import json
from typing import Dict, List, Mapping, Optional, Sequence


CUMULATIVE_LINES_SCHEMA_VERSION = 2


def encode_cumulative_lines(
    lines: Sequence[Mapping[str, object]],
    region_mappings: Sequence[Mapping[str, object]],
) -> List[Dict[str, object]]:
    """Replaces inline decisions with indices into the fixed region-mappings list."""

    indices = {}
    for index, decision in enumerate(region_mappings):
        key = _decision_key(decision)
        if key in indices:
            raise ValueError("Duplicate region decision in mapping evidence")
        indices[key] = index
    encoded = []
    for line in lines:
        region_ids = []
        for decision in line["regions"]:
            if decision.get("repo_relative_path") != line.get("repo_relative_path"):
                raise ValueError("Region decision belongs to another file")
            key = _decision_key(decision)
            if key not in indices:
                raise ValueError("Region decision is missing from mapping evidence")
            region_ids.append(indices[key])
        encoded.append({
            **{key: value for key, value in line.items() if key != "regions"},
            "region_ids": region_ids,
        })
    return encoded


def decode_cumulative_lines(
    coverage: Mapping[str, object],
    mapping_evidence: Optional[Mapping[str, object]] = None,
) -> List[Dict[str, object]]:
    """Reads legacy inline lines or validates and resolves schema-2 region indices."""

    version = coverage.get("schema_version", 1)
    lines = coverage.get("lines")
    if type(version) is not int or version not in (1, CUMULATIVE_LINES_SCHEMA_VERSION):
        raise ValueError("Unsupported cumulative-line schema")
    if not isinstance(lines, list) or any(not isinstance(line, dict) for line in lines):
        raise ValueError("Invalid cumulative lines")
    if version == 1:
        if any("region_ids" in line for line in lines):
            raise ValueError("Region references require cumulative-line schema 2")
        return lines
    table = mapping_evidence.get("region_mappings") if isinstance(mapping_evidence, dict) else None
    if not isinstance(table, list) or any(
        not isinstance(decision, dict)
        or not isinstance(decision.get("current_region"), dict)
        or not isinstance(decision.get("repo_relative_path"), str)
        for decision in table
    ):
        raise ValueError("Invalid region-mappings table")
    decoded = []
    for line in lines:
        region_ids = line.get("region_ids")
        if "regions" in line or not isinstance(region_ids, list):
            raise ValueError("Invalid region references")
        regions = []
        for index in region_ids:
            if type(index) is not int or index < 0 or index >= len(table):
                raise ValueError("Invalid region reference index")
            decision = table[index]
            if decision["repo_relative_path"] != line.get("repo_relative_path"):
                raise ValueError("Region reference belongs to another file")
            regions.append(decision)
        decoded.append({
            **{key: value for key, value in line.items() if key != "region_ids"},
            "regions": regions,
        })
    return decoded


def _decision_key(decision: Mapping[str, object]) -> bytes:
    """Binds an index to the complete decision, including all historical evidence."""

    return hashlib.sha256(json.dumps(
        decision, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")).digest()
