#!/usr/bin/env python3
"""Read-only, completion-verified projections for the local coverage report UI."""

import hashlib
import json
import re
import uuid
from pathlib import Path, PurePosixPath
from typing import Dict, List, Mapping, Tuple

from ioscoverage.storage.build_evidence import BuildEvidenceStore
from ioscoverage.storage.retained_evidence import RetainedEvidenceError
from ioscoverage.storage.analysis_state import AnalysisStateStore, AnalysisStateStoreError


SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
MAX_REPORT_JSON_BYTES = 128 * 1024 * 1024


class CoverageReportError(Exception):
    """Carries one bounded report-read error safe for an HTTP response."""

    def __init__(self, code: str, message: str):
        """Creates a stable report error without exposing local paths."""

        super().__init__(message)
        self.code = code
        self.message = message


class CoverageReportStore:
    """Loads only the latest completion-protected report selected by persisted state."""

    def __init__(self, analysis_root: Path, state_store: AnalysisStateStore):
        """Owns fixed analysis roots; callers cannot override paths per request."""

        self.analysis_root = analysis_root.resolve()
        self.orchestration_root = self.analysis_root / "cycle-latest"
        self.cycle_coverage_root = self.analysis_root / "cycle-coverage"
        self.state_store = state_store

    def get_report(self, cycle_id: str) -> Dict[str, object]:
        """Returns summary and file identities for the latest successful generation."""

        report = self._load_latest(cycle_id)
        summary = report["summary"]
        lines = summary.get("lines")
        if not isinstance(lines, dict):
            raise CoverageReportError(
                "report_format_unsupported", "Cycle line summary is invalid"
            )
        return {
            "schema_version": 1,
            "cycle_id": report["cycle_id"],
            "generation": report["generation"],
            "status": "complete",
            "updated_at": report["updated_at"],
            "current_build_id": report["current_build_id"],
            "orchestration_input_sha256": report["orchestration_input_sha256"],
            "cycle_input_set_sha256": report["cycle_input_set_sha256"],
            "summary": {
                "changed_new_side": lines["changed_new_side"],
                "measurable": lines["measurable"],
                "covered": lines["covered"],
                "uncovered": lines["uncovered"],
                "not_measurable": lines["not_measurable"],
                "unknown": lines["unknown"],
                "deleted_excluded": lines["deleted_excluded"],
                "covered_with_inherited_evidence": lines[
                    "covered_with_inherited_evidence"
                ],
                "changed_line_coverage_percent": summary[
                    "changed_line_coverage_percent"
                ],
            },
            "files": report["files"],
        }

    def get_mr_report_snapshot(self, cycle_id: str, expected: Mapping[str, object]):
        """Projects all strict-path lines in one validated immutable read for MR display."""

        loaded = self._load_latest(cycle_id)
        for field in ("generation", "current_build_id", "orchestration_input_sha256", "cycle_input_set_sha256"):
            if loaded.get(field) != expected.get(field):
                raise CoverageReportError("report_identity_mismatch", "Report changed during MR projection")
        details = {}
        for file in loaded["files"]:
            details[file["file_id"]] = {"file": file, "lines": []}
        for line in loaded["changed_lines"]:
            file_id = hashlib.sha256(line["repo_relative_path"].encode("utf-8")).hexdigest()
            details[file_id]["lines"].append(self._line_document(line))
        return {"files": loaded["files"], "summary": expected["summary"], "details": details}

    def get_mr_evidence_references(self, cycle_id, report):
        """Returns independent evidence not already incorporated by the formal report."""

        cycle_id = self._normalize_uuid(cycle_id)
        store = BuildEvidenceStore(self.analysis_root)
        try:
            references = store.references(cycle_id)
        except (OSError, ValueError) as error:
            raise CoverageReportError("report_result_invalid", "Independent evidence identity is unavailable") from error
        if not references:
            return []
        directory = self.orchestration_root / cycle_id / report["orchestration_input_sha256"]
        self._validate_result_directory(directory, "complete.json", "orchestration_input_sha256", report["orchestration_input_sha256"])
        formal = self._load_json(directory / "results.json").get("single_build_results", [])
        formal_by_build = {item["build_id"]: item for item in formal}
        selected = []
        for reference in references:
            incorporated = formal_by_build.get(reference["build_id"])
            try:
                if incorporated is not None:
                    if incorporated["input_set_sha256"] == reference.get("input_set_sha256"):
                        continue
                    single = self.analysis_root / "single-build" / incorporated["build_id"] / incorporated["input_set_sha256"]
                    self._validate_reference(incorporated, single, "completion.json")
                    completion = self._load_json(single / "completion.json")
                    if self._file_identity(single / "inputs.json") != (
                            completion["outputs"]["inputs.json"]["size"], completion["outputs"]["inputs.json"]["sha256"]):
                        raise ValueError("formal inputs changed")
                    inputs = self._load_json(single / "inputs.json")
                    older = store.inputs(reference)
                    current_runs = {item["run_id"]: item for item in inputs["runs"]}
                    if (inputs.get("cycle_id") == cycle_id and inputs.get("build_id") == reference["build_id"] and
                            inputs.get("input_set_sha256") == incorporated["input_set_sha256"] and
                            all(current_runs.get(item["run_id"]) == item for item in older["runs"])):
                        continue
            except (CoverageReportError, OSError, ValueError, KeyError, TypeError):
                pass  # Keep the independent reference; full validation happens at projection time.
            selected.append(reference)
        return selected

    def get_mr_evidence(self, cycle_id: str, report: Mapping[str, object], independent_references=()):
        """Loads completion-bound independent exports; unavailable entries stay local failures.

        The orchestration is revalidated against the exact supplied report pointer.
        Returned payloads remain internal and never appear in a public response.
        """

        cycle_id = self._normalize_uuid(cycle_id)
        orchestration_sha = report.get("orchestration_input_sha256")
        if not self._is_sha256(orchestration_sha):
            raise CoverageReportError("report_identity_mismatch", "Report evidence identity is invalid")
        directory = self.orchestration_root / cycle_id / orchestration_sha
        self._validate_result_directory(directory, "complete.json", "orchestration_input_sha256", orchestration_sha)
        references = self._load_json(directory / "results.json").get("single_build_results")
        if not isinstance(references, list) or not references:
            raise CoverageReportError("report_result_invalid", "Report evidence references are invalid")
        evidence = []
        seen = set()
        for reference in references:
            if not isinstance(reference, dict):
                raise CoverageReportError("report_result_invalid", "Report evidence reference is invalid")
            build_id, input_sha = reference.get("build_id"), reference.get("input_set_sha256")
            if not self._is_uuid(build_id) or not self._is_sha256(input_sha) or build_id in seen:
                raise CoverageReportError("report_identity_mismatch", "Report Build evidence identity is invalid")
            seen.add(build_id)
            item = {"build_id": build_id, "input_set_sha256": input_sha, "payload": None}
            try:
                single = self.analysis_root / "single-build" / build_id / input_sha
                if Path(str(reference.get("output_directory"))).resolve() != single:
                    raise CoverageReportError("report_path_invalid", "Build evidence path is invalid")
                self._validate_reference(reference, single, "completion.json")
                self._validate_result_directory(single, "completion.json", "input_set_sha256", input_sha)
                inputs = self._load_json(single / "inputs.json")
                summary = self._load_json(single / "summary.json")
                if (summary.get("build_id") != build_id or summary.get("cycle_id") != cycle_id or
                        inputs.get("build_id") != build_id or inputs.get("cycle_id") != cycle_id or
                        inputs.get("input_set_sha256") != input_sha):
                    raise CoverageReportError("report_identity_mismatch", "Build evidence ownership is invalid")
                item["payload"] = self._load_json(single / "llvm-cov-export.json")
                item["inputs"] = inputs
                item["updated_at"] = summary.get("completed_at")
                item["export_sha256"] = self._file_identity(single / "llvm-cov-export.json")[1]
            except CoverageReportError:
                item["error"] = "evidence_unavailable"
            evidence.append(item)
        store = BuildEvidenceStore(self.analysis_root)
        for reference in independent_references:
            item = {"build_id": reference["build_id"], "input_set_sha256": reference.get("input_set_sha256"), "payload": None}
            try:
                single, validated = store.load(reference)
                item["inputs"] = validated["inputs"]
                item["updated_at"] = validated["completion"].get("completed_at")
                item["payload"] = self._load_json(single / "llvm-cov-export.json")
                item["export_sha256"] = self._file_identity(single / "llvm-cov-export.json")[1]
            except (CoverageReportError, RetainedEvidenceError, OSError, ValueError, KeyError):
                item["error"] = "evidence_unavailable"
            evidence.append(item)
        return evidence

    def get_file(self, cycle_id: str, file_id: str) -> Dict[str, object]:
        """Returns changed lines for one server-issued file identity, never a path input."""

        if not isinstance(file_id, str) or not SHA256_PATTERN.fullmatch(file_id):
            raise CoverageReportError("report_file_not_found", "report file was not found")
        report = self._load_latest(cycle_id)
        selected = next(
            (item for item in report["files"] if item["file_id"] == file_id), None
        )
        if selected is None:
            raise CoverageReportError("report_file_not_found", "report file was not found")
        path = selected["repo_relative_path"]
        changed_lines = [
            self._line_document(item)
            for item in report["changed_lines"]
            if item["repo_relative_path"] == path
        ]
        return {
            "schema_version": 1,
            "cycle_id": report["cycle_id"],
            "generation": report["generation"],
            "updated_at": report["updated_at"],
            "current_build_id": report["current_build_id"],
            "orchestration_input_sha256": report["orchestration_input_sha256"],
            "cycle_input_set_sha256": report["cycle_input_set_sha256"],
            "file": selected,
            "lines": changed_lines,
        }

    def _load_latest(self, cycle_id: str) -> Dict[str, object]:
        """Loads latest success, retrying once if its identity advances during the read."""

        cycle_id = self._normalize_uuid(cycle_id)
        state = self._read_state(cycle_id)
        for attempt in range(2):
            identity = self._state_identity(state)
            try:
                return self._load_state(cycle_id, state)
            except CoverageReportError:
                if attempt != 0:
                    raise
                newer_state = self._read_state(cycle_id)
                if self._state_identity(newer_state) == identity:
                    raise
                state = newer_state
        raise CoverageReportError("report_not_available", "Coverage report is unavailable")

    def _read_state(self, cycle_id: str) -> Mapping[str, object]:
        """Reads one persisted latest-success pointer as an immutable local snapshot."""

        try:
            return dict(self.state_store.get_state(cycle_id))
        except AnalysisStateStoreError as error:
            raise CoverageReportError(error.code, error.message) from error

    @staticmethod
    def _state_identity(state: Mapping[str, object]) -> Tuple[object, ...]:
        """Returns the fields that identify one immutable latest-success generation."""

        return (
            state.get("latest_success_generation"),
            state.get("latest_success_orchestration_sha256"),
            state.get("latest_success_cycle_input_sha256"),
            state.get("latest_success_current_build_id"),
        )

    def _load_state(
        self, cycle_id: str, state: Mapping[str, object]
    ) -> Dict[str, object]:
        """Validates both immutable completion directories selected by one state snapshot."""

        orchestration_sha = state["latest_success_orchestration_sha256"]
        cycle_input_sha = state["latest_success_cycle_input_sha256"]
        current_build_id = state["latest_success_current_build_id"]
        generation = state["latest_success_generation"]
        if (
            generation is None
            or not self._is_sha256(orchestration_sha)
            or not self._is_sha256(cycle_input_sha)
            or not self._is_uuid(current_build_id)
        ):
            raise CoverageReportError(
                "report_not_available", "Cycle does not have a completed coverage report"
            )

        orchestration_dir = self.orchestration_root / cycle_id / orchestration_sha
        self._validate_result_directory(
            orchestration_dir,
            "complete.json",
            "orchestration_input_sha256",
            orchestration_sha,
        )
        results = self._load_json(orchestration_dir / "results.json")
        cycle_reference = results.get("cycle_coverage_result")
        if not isinstance(cycle_reference, dict):
            raise CoverageReportError("report_result_invalid", "Cycle result reference is invalid")
        if cycle_reference.get("cycle_input_set_sha256") != cycle_input_sha:
            raise CoverageReportError("report_identity_mismatch", "Cycle result identity changed")

        cycle_dir = (
            self.cycle_coverage_root / cycle_id / current_build_id / cycle_input_sha
        )
        declared_directory = cycle_reference.get("output_directory")
        if not isinstance(declared_directory, str) or Path(declared_directory).resolve() != cycle_dir:
            raise CoverageReportError("report_path_invalid", "Cycle result path is invalid")
        self._validate_reference(cycle_reference, cycle_dir)
        self._validate_result_directory(
            cycle_dir, "complete.json", "cycle_input_set_sha256", cycle_input_sha
        )

        summary = self._load_json(cycle_dir / "summary.json")
        coverage = self._load_json(cycle_dir / "cumulative-line-coverage.json")
        if (
            summary.get("status") != "complete"
            or summary.get("cycle_id") != cycle_id
            or summary.get("current_build_id") != current_build_id
            or summary.get("cycle_input_set_sha256") != cycle_input_sha
        ):
            raise CoverageReportError("report_identity_mismatch", "Cycle summary identity changed")
        changed_lines = coverage.get("lines")
        if not isinstance(changed_lines, list):
            raise CoverageReportError("report_format_unsupported", "Changed-line data is invalid")
        files = self._file_documents(summary, changed_lines)
        return {
            "cycle_id": cycle_id,
            "generation": generation,
            "updated_at": state["completed_at"],
            "current_build_id": current_build_id,
            "orchestration_input_sha256": orchestration_sha,
            "cycle_input_set_sha256": cycle_input_sha,
            "summary": summary,
            "files": files,
            "changed_lines": changed_lines,
        }

    def _file_documents(
        self, summary: Mapping[str, object], changed_lines: List[object]
    ) -> List[Dict[str, object]]:
        """Builds stable file IDs and rejects paths outside repository-relative semantics."""

        per_file = summary.get("per_file")
        if not isinstance(per_file, list):
            raise CoverageReportError("report_format_unsupported", "Per-file summary is invalid")
        line_paths = set()
        for item in changed_lines:
            if not isinstance(item, dict):
                raise CoverageReportError("report_format_unsupported", "Changed-line item is invalid")
            line_paths.add(self._safe_repo_path(item.get("repo_relative_path")))
        files = []
        for item in per_file:
            if not isinstance(item, dict):
                raise CoverageReportError("report_format_unsupported", "File summary is invalid")
            path = self._safe_repo_path(item.get("repo_relative_path"))
            if path not in line_paths and int(item.get("changed_new_side", 0)) > 0:
                raise CoverageReportError("report_identity_mismatch", "File summary has no lines")
            files.append(
                {
                    "file_id": hashlib.sha256(path.encode("utf-8")).hexdigest(),
                    "repo_relative_path": path,
                    "git_status": item.get("git_status"),
                    "changed_new_side": item.get("changed_new_side"),
                    "measurable": item.get("measurable"),
                    "covered": item.get("covered"),
                    "uncovered": item.get("uncovered"),
                    "not_measurable": item.get("not_measurable"),
                    "unknown": item.get("unknown"),
                    "deleted_excluded": item.get("deleted_excluded"),
                }
            )
        return sorted(files, key=lambda item: item["repo_relative_path"])

    def _line_document(self, item: object) -> Dict[str, object]:
        """Projects one changed line and auditable current/historical region sources."""

        if not isinstance(item, dict):
            raise CoverageReportError("report_format_unsupported", "Changed-line item is invalid")
        regions = item.get("regions")
        if not isinstance(regions, list):
            regions = []
        region_documents = []
        source_kinds = set()
        for decision in regions:
            if not isinstance(decision, dict) or not isinstance(
                decision.get("current_region"), dict
            ):
                raise CoverageReportError("report_format_unsupported", "Region item is invalid")
            region = decision["current_region"]
            source = decision.get("source") if isinstance(decision.get("source"), dict) else None
            source_document = None
            if source is not None:
                kind = source.get("kind")
                if kind == "current_build":
                    normalized_kind = "current_build"
                elif kind == "inherited_build":
                    normalized_kind = "historical_inheritance"
                else:
                    raise CoverageReportError(
                        "report_format_unsupported", "Region source kind is invalid"
                    )
                source_kinds.add(normalized_kind)
                source_document = {
                    "kind": normalized_kind,
                    "build_id": source.get("build_id"),
                    "input_set_sha256": source.get("input_set_sha256"),
                    "git_sha": source.get("git_sha"),
                }
            region_documents.append(
                {
                    "start_line": region.get("start_line"),
                    "start_column": region.get("start_column"),
                    "end_line": region.get("end_line"),
                    "end_column": region.get("end_column"),
                    "count": region.get("count"),
                    "covered": bool(decision.get("covered")),
                    "reason": decision.get("reason"),
                    "source": source_document,
                }
            )
        if source_kinds == {"current_build"}:
            classification = "current_build"
        elif source_kinds == {"historical_inheritance"}:
            classification = "historical_inheritance"
        elif len(source_kinds) > 1:
            classification = "mixed"
        else:
            classification = "none"
        return {
            "new_line_number": item.get("new_line_number"),
            "source_text": item.get("source_text"),
            "source_text_sha256": item.get("source_text_sha256"),
            "state": item.get("state"),
            "reason": item.get("reason"),
            "coverage_source": classification,
            "regions": region_documents,
        }

    def _validate_result_directory(
        self,
        directory: Path,
        completion_name: str,
        identity_field: str,
        identity_value: str,
    ) -> None:
        """Verifies every completion-declared file before serving report bytes."""

        if not directory.is_dir() or directory.is_symlink():
            raise CoverageReportError("report_not_available", "Coverage result is missing")
        completion = self._load_json(directory / completion_name)
        if (
            completion.get("status") != "complete"
            or completion.get(identity_field) != identity_value
            or not isinstance(completion.get("outputs"), dict)
        ):
            raise CoverageReportError("report_completion_invalid", "Coverage completion is invalid")
        outputs = completion["outputs"]
        actual = {
            path.relative_to(directory).as_posix()
            for path in directory.rglob("*")
            if path.is_file() and path.name != completion_name
        }
        if actual != set(outputs):
            raise CoverageReportError("report_completion_invalid", "Coverage output set changed")
        for relative, descriptor in outputs.items():
            safe = self._safe_relative_name(relative)
            if not isinstance(descriptor, dict):
                raise CoverageReportError("report_completion_invalid", "Coverage identity is invalid")
            size, digest = self._file_identity(directory / safe)
            if size != descriptor.get("size") or digest != descriptor.get("sha256"):
                raise CoverageReportError("report_completion_invalid", "Coverage output changed")

    def _validate_reference(self, reference: Mapping[str, object], directory: Path, completion_name: str = "complete.json") -> None:
        """Checks the orchestration pointer to the cumulative completion and summary."""

        for key, filename in (("completion", completion_name), ("summary", "summary.json")):
            descriptor = reference.get(key)
            if not isinstance(descriptor, dict):
                raise CoverageReportError("report_result_invalid", "Cycle reference is invalid")
            size, digest = self._file_identity(directory / filename)
            if size != descriptor.get("size") or digest != descriptor.get("sha256"):
                raise CoverageReportError("report_result_invalid", "Cycle reference changed")

    def _load_json(self, path: Path) -> Dict[str, object]:
        """Loads one bounded regular JSON object without following a symlink."""

        if not path.is_file() or path.is_symlink():
            raise CoverageReportError("report_file_missing", "Coverage report file is missing")
        try:
            if path.stat().st_size > MAX_REPORT_JSON_BYTES:
                raise CoverageReportError("report_file_too_large", "Coverage report is too large")
            payload = json.loads(path.read_text(encoding="utf-8"))
        except CoverageReportError:
            raise
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
            raise CoverageReportError("report_file_invalid", "Coverage report is invalid") from error
        if not isinstance(payload, dict):
            raise CoverageReportError("report_file_invalid", "Coverage report must be an object")
        return payload

    def _file_identity(self, path: Path) -> Tuple[int, str]:
        """Hashes one required regular non-symlink file."""

        if not path.is_file() or path.is_symlink():
            raise CoverageReportError("report_file_missing", "Coverage report file is missing")
        digest = hashlib.sha256()
        size = 0
        try:
            with path.open("rb") as source:
                while True:
                    chunk = source.read(1024 * 1024)
                    if not chunk:
                        break
                    size += len(chunk)
                    digest.update(chunk)
        except OSError as error:
            raise CoverageReportError("report_file_invalid", "Coverage report cannot be read") from error
        return size, digest.hexdigest()

    def _safe_relative_name(self, value: object) -> str:
        """Rejects completion entries capable of escaping their immutable directory."""

        if not isinstance(value, str) or not value:
            raise CoverageReportError("report_path_invalid", "Coverage output path is invalid")
        path = PurePosixPath(value)
        if path.is_absolute() or path.as_posix() != value or ".." in path.parts:
            raise CoverageReportError("report_path_invalid", "Coverage output path is unsafe")
        return value

    def _safe_repo_path(self, value: object) -> str:
        """Accepts only exact repository-relative paths already present in Git evidence."""

        if not isinstance(value, str) or not value:
            raise CoverageReportError("report_path_invalid", "Repository path is invalid")
        path = PurePosixPath(value)
        if path.is_absolute() or path.as_posix() != value or ".." in path.parts:
            raise CoverageReportError("report_path_invalid", "Repository path is unsafe")
        return value

    def _normalize_uuid(self, value: str) -> str:
        """Requires one canonical Cycle UUID before deriving any result path."""

        try:
            normalized = str(uuid.UUID(value))
        except (ValueError, AttributeError, TypeError) as error:
            raise CoverageReportError("invalid_uuid", "cycle_id must be a UUID") from error
        if normalized != value:
            raise CoverageReportError("invalid_uuid", "cycle_id must be canonical")
        return normalized

    def _is_uuid(self, value: object) -> bool:
        """Returns whether a value is one canonical UUID string."""

        try:
            return isinstance(value, str) and str(uuid.UUID(value)) == value
        except (ValueError, AttributeError, TypeError):
            return False

    def _is_sha256(self, value: object) -> bool:
        """Returns whether a value is one lowercase SHA-256 identity."""

        return isinstance(value, str) and SHA256_PATTERN.fullmatch(value) is not None
