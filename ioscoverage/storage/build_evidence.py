"""Atomic references to independent Build results, separate from formal Cycle success."""

import hashlib
import json
import os
import re
import tempfile
import uuid
from pathlib import Path

from ioscoverage.storage.retained_evidence import RetainedBuildEvidenceStore

SHA256 = re.compile(r"[0-9a-f]{64}")


class BuildEvidenceStore:
    """One scheduler writer publishes per-Build pointers; readers never schedule or mutate.

    The process/runtime analysis lock serializes publication against cleanup.
    A pointer references immutable bytes and never authorizes raw-input deletion.
    """

    def __init__(self, analysis_root):
        """Derives fixed paths and reuses existing single-Build completion validation."""

        self.analysis_root = Path(analysis_root).resolve()
        self.root = self.analysis_root / "build-evidence"
        self.validator = RetainedBuildEvidenceStore(self.analysis_root)

    @staticmethod
    def _uuid(value):
        """Requires canonical IDs before deriving any runtime path."""

        if not isinstance(value, str) or str(uuid.UUID(value)) != value:
            raise ValueError("invalid independent evidence identity")
        return value

    def references(self, cycle_id):
        """Reads bounded atomic reference documents, retaining local invalidity markers."""

        cycle_id = self._uuid(cycle_id)
        directory = self.root / cycle_id
        if self.root.is_symlink() or directory.is_symlink():
            raise ValueError("unsafe independent evidence directory")
        if not directory.is_dir():
            return []
        references = []
        for path in sorted(directory.glob("*.json")):
            build_id = self._uuid(path.stem)
            invalid = {"cycle_id": cycle_id, "build_id": build_id, "error": "evidence_unavailable"}
            try:
                if path.is_symlink() or path.stat().st_size > 4096:
                    raise ValueError("unsafe independent evidence reference")
                payload = json.loads(path.read_text(encoding="utf-8"))
                if (not isinstance(payload, dict) or set(payload) != {
                        "schema_version", "cycle_id", "build_id", "input_set_sha256", "completion_sha256"} or
                        type(payload["schema_version"]) is not int or payload["schema_version"] != 1 or payload["cycle_id"] != cycle_id or payload["build_id"] != build_id or
                        any(not isinstance(payload[field], str) or SHA256.fullmatch(payload[field]) is None
                            for field in ("input_set_sha256", "completion_sha256"))):
                    raise ValueError("invalid independent evidence reference")
                references.append(payload)
            except (OSError, UnicodeError, ValueError):
                references.append(invalid)
        return references

    def inputs(self, reference):
        """Reads completion-bound input metadata without hashing the large profile."""

        cycle_id, build_id = self._uuid(reference["cycle_id"]), self._uuid(reference["build_id"])
        input_sha = reference.get("input_set_sha256")
        if not isinstance(input_sha, str) or SHA256.fullmatch(input_sha) is None:
            raise ValueError("independent evidence is unavailable")
        directory = self.analysis_root / "single-build" / build_id / input_sha
        if directory.resolve() != directory or directory.is_symlink():
            raise ValueError("unsafe independent evidence source")
        completion_path, inputs_path = directory / "completion.json", directory / "inputs.json"
        for path in (completion_path, inputs_path):
            if path.is_symlink() or path.stat().st_size > 128 * 1024 * 1024:
                raise ValueError("unsafe independent evidence metadata")
        completion_bytes = completion_path.read_bytes()
        if hashlib.sha256(completion_bytes).hexdigest() != reference.get("completion_sha256"):
            raise ValueError("independent evidence completion changed")
        completion = json.loads(completion_bytes)
        if (not isinstance(completion, dict) or completion.get("status") != "complete" or
                completion.get("input_set_sha256") != input_sha or not isinstance(completion.get("outputs"), dict)):
            raise ValueError("independent completion metadata is invalid")
        inputs_bytes = inputs_path.read_bytes()
        descriptor = {"size": len(inputs_bytes), "sha256": hashlib.sha256(inputs_bytes).hexdigest()}
        if completion.get("outputs", {}).get("inputs.json") != descriptor:
            raise ValueError("independent input metadata changed")
        inputs = json.loads(inputs_bytes)
        if (not isinstance(inputs, dict) or inputs.get("cycle_id") != cycle_id or inputs.get("build_id") != build_id or
                inputs.get("input_set_sha256") != input_sha):
            raise ValueError("independent input identity changed")
        return inputs

    def load(self, reference):
        """Validates an issued reference and every immutable output before use."""

        cycle_id, build_id = self._uuid(reference["cycle_id"]), self._uuid(reference["build_id"])
        input_sha = reference.get("input_set_sha256")
        if not isinstance(input_sha, str) or SHA256.fullmatch(input_sha) is None:
            raise ValueError("independent evidence is unavailable")
        directory = self.analysis_root / "single-build" / build_id / input_sha
        validated = self.validator.validate_result(directory, cycle_id, build_id)
        completion_sha = hashlib.sha256((directory / "completion.json").read_bytes()).hexdigest()
        if completion_sha != reference.get("completion_sha256"):
            raise ValueError("independent evidence completion changed")
        return directory, validated

    def publish(self, cycle_id, build_id, result_directory):
        """Publishes only a completed independent result, with atomic/fsynced pointer replacement."""

        cycle_id, build_id = self._uuid(cycle_id), self._uuid(build_id)
        directory = Path(result_directory)
        validated = self.validator.validate_result(directory, cycle_id, build_id)
        payload = {"schema_version": 1, "cycle_id": cycle_id, "build_id": build_id,
                   "input_set_sha256": validated["inputs"]["input_set_sha256"],
                   "completion_sha256": hashlib.sha256((directory / "completion.json").read_bytes()).hexdigest()}
        destination = self.root / cycle_id / (build_id + ".json")
        if self.root.is_symlink() or destination.parent.is_symlink() or destination.is_symlink():
            raise ValueError("unsafe independent evidence publication")
        destination.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(prefix=".build-", dir=str(destination.parent))
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as output:
                json.dump(payload, output, sort_keys=True)
                output.write("\n")
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, destination)
            for parent in (destination.parent, self.root, self.analysis_root):
                descriptor = os.open(str(parent), os.O_RDONLY)
                try:
                    os.fsync(descriptor)
                finally:
                    os.close(descriptor)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
        return payload
