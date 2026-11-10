"""Read-only current-MR projection from independent, verified Build evidence."""

import hashlib
from collections import Counter

from ioscoverage.analysis.diff.git_diff_parser import GitDiffParseError, parse_name_status_z, parse_unified_zero_patch
from ioscoverage.storage.coverage_store import CoverageStoreError
from ioscoverage.analysis.mapping.coverage_export import (
    CoverageExportError, fixed_git_sources, normalize_coverage_export, regions_for_source_line,
)
from ioscoverage.analysis.mapping.source_region import (
    SourceRegionMappingError, build_file_line_mapping, select_file_diff,
)
from ioscoverage.analysis.mapping.mr_lines import verified_line_mapping
from ioscoverage.integrations.git_repository import GitRepositoryError

LOCAL_ERRORS = (CoverageStoreError, GitRepositoryError, GitDiffParseError, SourceRegionMappingError, CoverageExportError,
                UnicodeError, ValueError, KeyError, TypeError, IndexError)
STATES = ("covered", "uncovered", "not_measurable", "unknown")


def counts(lines):
    """Counts presentation categories without inventing a compiled denominator."""

    states = Counter(line["state"] for line in lines)
    return {"changed_new_side": len(lines), **{state: states[state] for state in STATES},
            "measurable": 0, "changed_line_coverage_percent": None}


class MergeRequestPresentation:
    """Owns one request's fixed Git sources, line maps and per-Build LLVM evidence."""

    def __init__(self, repository, definition, coverage_store):
        """Uses only the registry-selected repository and existing immutable store."""

        self.repository, self.definition, self.coverage_store = repository, definition, coverage_store

    def project(self, cycle_id, diff, evidence, published=None):
        """Maps each independent Build directly to current code, never via the last MR."""

        self.repository.validate_build_snapshot(diff["head_sha"], diff["start_sha"], diff["base_sha"])
        self.retryable = False
        self.rename_checks = {}
        current_diff = self._diff(diff["base_sha"], diff["head_sha"])
        prepared = []
        for item in evidence:
            try:
                build = self.coverage_store.get_build(item["build_id"])
                if build["cycle_id"] != cycle_id or build["artifact_state"] != "ready":
                    raise ValueError("evidence ownership")
                inputs = item.get("inputs", {})
                identity = inputs.get("build", {})
                if item.get("payload") is None or inputs.get("cycle_id") != cycle_id or inputs.get("build_id") != build["id"]:
                    raise ValueError("evidence unavailable")
                if any(identity.get(field) != build.get(field) for field in
                       ("git_sha", "target_sha_at_build", "merge_base_sha", "manifest_sha256", "coverage_binary")):
                    raise ValueError("evidence Build changed")
                sha = build["git_sha"]
                paths, source = fixed_git_sources(
                    lambda label, args: self.repository._run(args[3:]),
                    self.repository.path, sha, "mr")
                coverage = normalize_coverage_export(item["payload"], self.repository.path, paths, source,
                    self.definition.compiler_repository_names or (self.repository.path.name,))
                prepared.append({"build": build, "item": item, "coverage": coverage, "source": source,
                                 "target_sha": diff["head_sha"], "diff": self._diff(sha, diff["head_sha"])})
            except LOCAL_ERRORS:
                self.retryable = True
                prepared.append(None)
        if published is not None:
            try:
                build = self.coverage_store.get_build(published["build_id"])
                if build["cycle_id"] != cycle_id or build["artifact_state"] != "ready":
                    raise ValueError("published evidence ownership")
                prepared.append({"build": build, "source": lambda path, sha=build["git_sha"]: self.repository.source(sha, path),
                    "published": {detail["file"]["repo_relative_path"]: {
                        line["new_line_number"]: line for line in detail["lines"]}
                        for detail in published["details"].values()},
                    "target_sha": diff["head_sha"], "diff": self._diff(build["git_sha"], diff["head_sha"])})
            except LOCAL_ERRORS:
                self.retryable = True
                prepared.append(None)
        files, details = [], {}
        for changed in current_diff:
            if changed.new_path is None or changed.binary or not changed.changed_new_lines:
                continue
            path = changed.new_path
            lines = []
            try:
                current = self.repository.source(diff["head_sha"], path)
                current_lines = current.splitlines()
                decisions = [self._file_evidence(path, current, entry) for entry in prepared]
                for number in changed.changed_new_lines:
                    if not 0 < number <= len(current_lines):
                        raise ValueError("current range")
                    options = [self._line_evidence(number, decision) for decision in decisions]
                    hit = next((value for value in options if value["state"] == "covered"), None)
                    if hit is not None:
                        decision = hit
                    elif any(value["state"] == "unknown" for value in options):
                        decision = {"state": "unknown", "reason": "mapping_or_evidence_unavailable"}
                    elif any(value.get("measured") for value in options):
                        decision = {"state": "uncovered", "reason": "no_applicable_complete_hit"}
                    elif any(value["state"] == "not_measurable" for value in options):
                        decision = {"state": "not_measurable", "reason": "unchanged_compiler_classification"}
                    else:
                        decision = {"state": "uncovered", "reason": "no_applicable_complete_hit"}
                    lines.append({"new_line_number": number, "regions": [], "coverage_source": "none", **decision})
            except LOCAL_ERRORS:
                self.retryable = True
                lines = [{"new_line_number": number, "state": "unknown", "coverage_source": "none",
                          "regions": [], "reason": "file_data_unavailable"} for number in changed.changed_new_lines]
            file_id = hashlib.sha256(path.encode()).hexdigest()
            descriptor = {"file_id": file_id, "repo_relative_path": path, **counts(lines)}
            files.append(descriptor)
            details[file_id] = {"file": descriptor, "lines": lines}
        all_lines = [line for detail in details.values() for line in detail["lines"]]
        return {"files": files, "summary": counts(all_lines), "details": details, "retryable": self.retryable}

    def _diff(self, old_sha, new_sha):
        """Parses only the registry repository's deterministic fixed-object diff."""

        status, patch = self.repository.diff_text(old_sha, new_sha)
        return parse_unified_zero_patch(patch, parse_name_status_z(status))

    def _file_evidence(self, path, current, entry):
        """Resolves only a Git unchanged path or one-to-one rename; never searches text."""

        if entry is None:
            return None
        try:
            change = select_file_diff(path, entry["diff"])
            if change is not None and change.status == "T":
                return None
            if change is not None and change.status == "A":
                return {"new": True}
            if change is not None and change.status == "R" and not self._unique_rename(change, entry):
                return None
            historical_path = change.old_path if change else path
            old = entry["source"](historical_path)
            mapping = build_file_line_mapping(path, old, current, change)
            valid, ambiguous = verified_line_mapping(old, current, mapping.current_to_historical_lines)
            return {**entry, "path": historical_path, "old": old.splitlines(), "mapping": valid, "ambiguous": ambiguous}
        except LOCAL_ERRORS:
            self.retryable = True
            return None

    def _unique_rename(self, change, entry):
        """Vetoes competing fixed path identities without penalizing unrelated files.

        Git supplies both the positive rename/hunk identity and the competing
        pair decisions; source similarity in this layer never creates green.
        """

        sources = (item.old_path for item in entry["diff"] if item.status in ("D", "R"))
        targets = (item.new_path for item in entry["diff"] if item.status in ("A", "R"))
        return not any(path != change.old_path and self._competing_rename(entry, path, change.new_path)
                       for path in sources) and not any(
            path != change.new_path and self._competing_rename(entry, change.old_path, path) for path in targets)

    def _competing_rename(self, entry, old_path, new_path):
        """Checks each relevant pair once with Git's fixed, literal-path rename operation."""

        key = (entry["build"]["git_sha"], entry["target_sha"], old_path, new_path)
        if key not in self.rename_checks:
            status = parse_name_status_z(self.repository.rename_status(*key))
            self.rename_checks[key] = any(item.status == "R" and item.old_path == old_path and
                                          item.new_path == new_path for item in status)
        return self.rename_checks[key]

    def _line_evidence(self, number, entry):
        """Requires a complete same-Build line hit; edits never inherit an old gray state."""

        if entry is None or number in entry.get("ambiguous", ()):
            return {"state": "unknown"}
        if entry.get("new") or number not in entry["mapping"]:
            return {"state": "uncovered"}
        path = entry["path"]
        if "published" in entry:
            line = entry["published"].get(path, {}).get(entry["mapping"][number])
            if line and line["state"] == "covered" and line["regions"] and all(region["covered"] for region in line["regions"]):
                return {"state": "covered", "coverage_source": line["coverage_source"], "regions": line["regions"]}
            return {"state": "uncovered"}
        coverage = entry["coverage"]
        if path in coverage.conflicted_paths:
            return {"state": "unknown"}
        related, error = regions_for_source_line(coverage.regions_by_path.get(path, ()),
                                                entry["mapping"][number], entry["old"])
        if error:
            return {"state": "unknown"}
        if not related:
            return {"state": "not_measurable"}
        if not all(region.count > 0 for region in related):
            return {"state": "uncovered", "measured": True}
        return {"state": "covered", "coverage_source": "historical_inheritance",
                "evidence": {"build_id": entry["build"]["id"], "git_sha": entry["build"]["git_sha"],
                             "input_set_sha256": entry["item"]["input_set_sha256"],
                             "export_sha256": entry["item"]["export_sha256"],
                             **({"reported_at": entry["item"]["updated_at"]} if entry["item"].get("updated_at") else {})}}
