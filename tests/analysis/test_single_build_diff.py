#!/usr/bin/env python3
"""Focused tests for fixed-SHA single-Build changed-line coverage analysis."""

import hashlib
import json
import sqlite3
import subprocess
import tempfile
import unittest
import uuid
from pathlib import Path
from typing import Dict, List, Optional, Sequence
from unittest.mock import patch

from ioscoverage.analysis.mapping import coverage_export
from ioscoverage.analysis.mapping.coverage_export import (
    CoverageExportError,
    CoverageRegion,
    normalize_coverage_export,
    regions_for_source_line,
)
from ioscoverage.analysis.diff.git_diff_parser import (
    GitDiffParseError,
    GitFileStatus,
    parse_name_status_z,
    parse_unified_zero_patch,
    validate_repo_relative_path,
)
from ioscoverage.analysis.single_build.analyzer import AnalysisConfiguration
from ioscoverage.analysis.diff.analyzer import (
    SingleBuildDiffAnalysisError,
    SingleBuildDiffAnalyzer,
)
from ioscoverage.storage.retained_evidence import RetainedBuildEvidenceStore


class FakeGitController:
    """Provides deterministic Git output and selected process failures."""

    def __init__(self, repository: Path, head: str, merge_base: str) -> None:
        """Creates one normal modified-file diff fixture."""

        self.repository = repository
        self.head = head
        self.merge_base = merge_base
        self.status = "M\x00Sources/File.swift\x00"
        self.patch = (
            "diff --git a/Sources/File.swift b/Sources/File.swift\n"
            "index 1111111..2222222 100644\n"
            "--- a/Sources/File.swift\n"
            "+++ b/Sources/File.swift\n"
            "@@ -1 +1,3 @@\n"
            "-let oldValue = 0\n"
            "+let coveredValue = 1\n"
            "+let mixedValue = condition ? 1 : 0\n"
            "+// changed comment\n"
        )
        self.sources = {
            "Sources/File.swift": (
                "let coveredValue = 1\n"
                "let mixedValue = condition ? 1 : 0\n"
                "// changed comment\n"
            )
        }
        self.failure_label: Optional[str] = None
        self.stderr_label: Optional[str] = None
        self.dirty = False
        self.repository_root = str(repository)
        self.runners: List[FakeGitRunner] = []

    def runner_factory(self, evidence_dir: Path, developer_dir: Path) -> "FakeGitRunner":
        """Retains each staged runner for command assertions."""

        runner = FakeGitRunner(evidence_dir, developer_dir, self)
        self.runners.append(runner)
        return runner


class FakeGitRunner:
    """Matches EvidenceCommandRunner while never invoking a subprocess."""

    def __init__(
        self, evidence_dir: Path, developer_dir: Path, controller: FakeGitController
    ) -> None:
        """Stores fixed evidence paths and the shared output controller."""

        self.evidence_dir = evidence_dir
        self.developer_dir = developer_dir
        self.controller = controller
        self.records: List[Dict[str, object]] = []
        self.labels: List[str] = []

    def run(
        self,
        label: str,
        arguments: Sequence[str],
        stdout_filename: Optional[str] = None,
        timeout_seconds: int = 300,
    ) -> subprocess.CompletedProcess:
        """Writes deterministic command evidence including NUL status output."""

        self.labels.append(label)
        stdout = self._stdout(label, arguments)
        stderr = "fixture warning\n" if label == self.controller.stderr_label else ""
        returncode = 1 if label == self.controller.failure_label else 0
        stdout_name = stdout_filename or label + ".stdout.txt"
        stdout_path = self.evidence_dir / stdout_name
        stdout_path.write_text(stdout, encoding="utf-8")
        stderr_path = self.evidence_dir / (label + ".stderr.txt")
        stderr_path.write_text(stderr, encoding="utf-8")
        self.records.append(
            {
                "label": label,
                "arguments": list(arguments),
                "environment": {"DEVELOPER_DIR": str(self.developer_dir)},
                "started_at": "2026-09-01T00:00:00+00:00",
                "completed_at": "2026-09-01T00:00:01+00:00",
                "exit_code": returncode,
                "timed_out": False,
                "stdout": stdout_name,
                "stderr": stderr_path.name,
                "stdout_size": stdout_path.stat().st_size,
                "stderr_size": stderr_path.stat().st_size,
            }
        )
        completed_stdout = "" if stdout_filename else stdout
        return subprocess.CompletedProcess(
            list(arguments), returncode, stdout=completed_stdout, stderr=stderr
        )

    def _stdout(self, label: str, arguments: Sequence[str]) -> str:
        """Returns the selected output for each fixed command label."""

        if label == "git-merge-base":
            return self.controller.merge_base + "\n"
        if label == "git-coverage-tree":
            return "\x00".join(sorted(self.controller.sources)) + "\x00"
        if label.startswith("git-coverage-source-"):
            return self.controller.sources[str(arguments[-1]).split(":", 1)[1]]
        if label == "git-repository-root":
            return self.controller.repository_root + "\n"
        if label == "git-head":
            return self.controller.head + "\n"
        if label == "git-worktree-status":
            return "?? dirty.txt\n" if self.controller.dirty else ""
        if label == "git-name-status":
            return self.controller.status
        if label == "git-diff":
            return self.controller.patch
        if label.startswith("git-show-"):
            object_name = str(arguments[-1])
            path = object_name.split(":", 1)[1]
            return self.controller.sources[path]
        return ""


class SingleBuildDiffAnalyzerTests(unittest.TestCase):
    """Covers diff parsing, coverage mapping, integrity, publication, and idempotency."""

    def setUp(self) -> None:
        """Creates isolated SQLite, Build artifacts, prior analysis, and fixed Git output."""

        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.repository = self.root / "repository"
        self.repository.mkdir()
        self.database = self.root / "instance" / "coverage.sqlite3"
        self.data_dir = self.root / "data"
        self.analysis_root = self.root / "analysis" / "single-build"
        self.cycle_id = str(uuid.uuid4())
        self.build_id = str(uuid.uuid4())
        self.input_sha = "f" * 64
        self.git_sha = "1" * 40
        self.target_sha = "2" * 40
        self.merge_base = "3" * 40
        self.main_bytes = b"main-macho"
        self.dsym_bytes = b"dsym-zip"
        self.macho_uuid = str(uuid.uuid4())
        self.controller = FakeGitController(self.repository, self.git_sha, self.merge_base)
        self.configuration = AnalysisConfiguration(
            database_path=self.database,
            data_dir=self.data_dir,
            analysis_root=self.analysis_root,
            repository_path=self.repository,
        )
        self._create_database_and_build()
        self._create_single_build_result(self._coverage_payload())

    def tearDown(self) -> None:
        """Removes every isolated fixture."""

        self.temporary.cleanup()

    def analyzer(self) -> SingleBuildDiffAnalyzer:
        """Creates an analyzer with controlled no-shell Git execution."""

        return SingleBuildDiffAnalyzer(
            self.configuration, command_runner_factory=self.controller.runner_factory
        )

    def test_normal_added_and_modified_lines_map_to_all_four_states(self) -> None:
        """Maps covered, mixed-uncovered, and compiler-unmapped lines conservatively."""

        result = self.analyzer().analyze(self.cycle_id, self.build_id, self.input_sha)
        self.assertEqual(result.summary["lines"]["covered"], 1)
        self.assertEqual(result.summary["lines"]["uncovered"], 1)
        self.assertEqual(result.summary["lines"]["not_measurable"], 1)
        self.assertEqual(result.summary["lines"]["deleted_excluded"], 1)
        details = json.loads((result.output_directory / "changed-line-coverage.json").read_text())
        self.assertEqual([line["state"] for line in details["lines"]], ["covered", "uncovered", "not_measurable"])

    def test_retained_dsym_can_be_absent_after_safe_cleanup(self) -> None:
        """Consumes a verified LLVM result after retained evidence authorizes dSYM removal."""

        evidence = RetainedBuildEvidenceStore(self.analysis_root.parent)
        evidence.publish(
            self.cycle_id, self.build_id, self._single_result_dir(), ["dsym"]
        )
        (self.data_dir / "cycles" / self.cycle_id / "builds" / self.build_id
         / "artifacts" / "dsym.zip").unlink()

        result = self.analyzer().analyze(
            self.cycle_id, self.build_id, self.input_sha
        )

        self.assertEqual(result.summary["status"], "complete")

    def test_retained_evidence_never_authorizes_missing_coverage_binary(self) -> None:
        """Keeps the selected Mach-O mandatory even after noncoverage artifacts are compacted."""

        evidence = RetainedBuildEvidenceStore(self.analysis_root.parent)
        evidence.publish(
            self.cycle_id, self.build_id, self._single_result_dir(), ["dsym"]
        )
        artifacts = (
            self.data_dir / "cycles" / self.cycle_id / "builds" / self.build_id
            / "artifacts"
        )
        (artifacts / "dsym.zip").unlink()
        (artifacts / "main_executable.bin").unlink()

        with self.assertRaises(SingleBuildDiffAnalysisError) as caught:
            self.analyzer().analyze(self.cycle_id, self.build_id, self.input_sha)

        self.assertEqual(caught.exception.code, "stored_file_missing")

    def test_name_status_parses_added_modified_and_renamed_files(self) -> None:
        """Parses NUL-delimited A/M/R identities without splitting path spaces."""

        parsed = parse_name_status_z("A\x00A.swift\x00M\x00B.swift\x00R091\x00Old.swift\x00New.swift\x00")
        self.assertEqual([item.status for item in parsed], ["A", "M", "R"])
        self.assertEqual(parsed[-1].similarity, 91)

    def test_pure_rename_has_no_changed_lines(self) -> None:
        """Leaves a 100-percent rename outside the changed-line denominator."""

        status = [GitFileStatus("R", "Old Name.swift", "New Name.swift", 100)]
        patch = (
            "diff --git a/Old Name.swift b/New Name.swift\n"
            "similarity index 100%\nrename from Old Name.swift\nrename to New Name.swift\n"
        )
        parsed = parse_unified_zero_patch(patch, status)
        self.assertEqual(parsed[0].changed_new_lines, ())

    def test_renamed_modified_file_uses_new_path_and_new_line(self) -> None:
        """Associates rename edits with the new repository path."""

        status = [GitFileStatus("R", "Old.swift", "New.swift", 80)]
        patch = (
            "diff --git a/Old.swift b/New.swift\n"
            "similarity index 80%\nrename from Old.swift\nrename to New.swift\n"
            "@@ -2 +2 @@\n-old\n+new\n"
        )
        parsed = parse_unified_zero_patch(patch, status)[0]
        self.assertEqual(parsed.new_path, "New.swift")
        self.assertEqual(parsed.changed_new_lines, (2,))

    def test_deleted_file_and_deleted_lines_are_excluded(self) -> None:
        """Counts removed lines only as deleted evidence, never current denominator lines."""

        status = [GitFileStatus("D", "Gone.swift", None, None)]
        patch = (
            "diff --git a/Gone.swift b/Gone.swift\n"
            "deleted file mode 100644\n--- a/Gone.swift\n+++ /dev/null\n"
            "@@ -1,2 +0,0 @@\n-one\n-two\n"
        )
        parsed = parse_unified_zero_patch(patch, status)[0]
        self.assertEqual(parsed.changed_new_lines, ())
        self.assertEqual(parsed.deleted_excluded, 2)

    def test_context_lines_are_not_changed_lines(self) -> None:
        """Does not treat unified context as an added new-side line."""

        status = [GitFileStatus("M", "File.swift", "File.swift", None)]
        patch = (
            "diff --git a/File.swift b/File.swift\n--- a/File.swift\n+++ b/File.swift\n"
            "@@ -1,2 +1,2 @@\n context\n-old\n+new\n"
        )
        parsed = parse_unified_zero_patch(patch, status)[0]
        self.assertEqual(parsed.changed_new_lines, (2,))

    def test_binary_file_has_no_measurable_lines(self) -> None:
        """Records a binary file identity without inventing source lines."""

        status = [GitFileStatus("M", "Image.png", "Image.png", None)]
        patch = "diff --git a/Image.png b/Image.png\nBinary files a/Image.png and b/Image.png differ\n"
        parsed = parse_unified_zero_patch(patch, status)[0]
        self.assertTrue(parsed.binary)
        self.assertEqual(parsed.changed_new_lines, ())

    def test_paths_with_spaces_are_preserved(self) -> None:
        """Keeps spaces in NUL-delimited repository paths."""

        parsed = parse_name_status_z("M\x00Sources/My File.swift\x00")
        self.assertEqual(parsed[0].new_path, "Sources/My File.swift")

    def test_absolute_git_path_is_rejected(self) -> None:
        """Rejects paths outside repository-relative semantics."""

        with self.assertRaises(GitDiffParseError):
            validate_repo_relative_path("/tmp/File.swift")

    def test_parent_escape_git_path_is_rejected(self) -> None:
        """Rejects `..` path traversal before Git object access."""

        with self.assertRaises(GitDiffParseError):
            validate_repo_relative_path("Sources/../Secret.swift")

    def test_coverage_absolute_path_maps_exactly_to_repository_relative_path(self) -> None:
        """Maps only an absolute compiler path provably beneath the fixed repository."""

        normalized = normalize_coverage_export(self._coverage_payload(), self.repository)
        self.assertIn("Sources/File.swift", normalized.regions_by_path)

    def test_coverage_basename_fallback_is_not_used(self) -> None:
        """Rejects a whole export whose external basename is its only code evidence."""

        payload = self._coverage_payload(filename="/tmp/elsewhere/File.swift")
        with self.assertRaises(CoverageExportError) as caught:
            normalize_coverage_export(payload, self.repository)
        self.assertEqual(caught.exception.code, "coverage_path_mapping_failed")

    def test_relative_coverage_preserves_covered_and_uncovered_git_lines(self) -> None:
        """Uses fixed Git paths directly without selecting a checkout root."""

        self._replace_export(self._coverage_payload("Sources/File.swift"))
        with patch.object(coverage_export, "_allowed_root_candidates",
                          side_effect=AssertionError("relative paths must not infer a root")):
            result = self.analyzer().analyze(self.cycle_id, self.build_id, self.input_sha)
        self.assertEqual(result.summary["lines"]["covered"], 1)
        self.assertEqual(result.summary["lines"]["uncovered"], 1)

    def test_relative_coverage_requires_fixed_tree_and_source(self) -> None:
        """Rejects relative input without immutable Git evidence or with aliases."""

        source = self.controller.sources["Sources/File.swift"]
        for paths, loader in ((("Sources/File.swift",), None), (None, lambda _: source)):
            with self.subTest(paths=paths), self.assertRaises(CoverageExportError):
                normalize_coverage_export(self._coverage_payload("Sources/File.swift"),
                                          self.repository, paths, loader)
        for path in ("./Sources/File.swift", "Sources/../Sources/File.swift",
                     "Sources//File.swift", "Sources\\File.swift", "Other/File.swift"):
            with self.subTest(path=path), self.assertRaises(CoverageExportError):
                normalize_coverage_export(self._coverage_payload(path), self.repository,
                                          ("Sources/File.swift",), lambda _: source)

    def test_relative_coverage_checks_fixed_blob_coordinates(self) -> None:
        """Rejects geometrically invalid regions even when the relative name exists."""

        with self.assertRaises(CoverageExportError) as caught:
            normalize_coverage_export(self._coverage_payload("Sources/File.swift"),
                                      self.repository, ("Sources/File.swift",), lambda _: "x\n")
        self.assertEqual(caught.exception.code, "coverage_path_source_mismatch")

    def test_local_symlink_after_canonical_file_keeps_resolved_mapping(self) -> None:
        """Preserves local symlink resolution after another file selects the root."""

        root = self.repository.resolve()
        (root / "Sources").mkdir(exist_ok=True)
        (root / "ZAlias").symlink_to("Sources", target_is_directory=True)
        first = self._coverage_payload(str(root / "A.swift"))
        alias = self._coverage_payload(str(root / "ZAlias/File.swift"))
        for key in ("files", "functions"):
            first["data"][0][key].extend(alias["data"][0][key])
        result = normalize_coverage_export(first, root,
                                          ("A.swift", "Sources/File.swift", "ZAlias"))
        self.assertEqual(set(result.regions_by_path), {"A.swift", "Sources/File.swift"})
        self.assertEqual(result.unmapped_files, ())

    def test_foreign_compiler_root_maps_to_fixed_git_source(self) -> None:
        """Maps a foreign root only through the registered tree and valid regions."""

        payload = self._coverage_payload("/builder/work/repository/Sources/File.swift")
        self._replace_export(payload)
        result = self.analyzer().analyze(self.cycle_id, self.build_id, self.input_sha)
        self.assertEqual(result.summary["lines"]["covered"], 1)
        self.assertEqual(result.summary["lines"]["uncovered"], 1)
        normalized = json.loads((result.output_directory / "normalized-coverage.json").read_text())
        self.assertEqual(normalized["files"][0]["repo_relative_path"], "Sources/File.swift")
        self.assertNotIn("/builder/work", json.dumps(normalized))

    def test_whole_export_path_miss_fails_without_success(self) -> None:
        """Fails with a safe code rather than publishing an all-unmeasurable diff."""

        self._replace_export(self._coverage_payload("/builder/work/other/Sources/File.swift"))
        with self.assertRaises(SingleBuildDiffAnalysisError) as caught:
            self.analyzer().analyze(self.cycle_id, self.build_id, self.input_sha)
        self.assertEqual(caught.exception.code, "coverage_path_mapping_failed")
        self.assertNotIn("/builder/", caught.exception.message)
        self.assertFalse((self.analysis_root.parent / "single-build-diff" / self.build_id
                          / self.input_sha).exists())

    def test_renamed_checkout_preserves_uncovered_lines_and_ignores_generated_source(self) -> None:
        """Uses a configured checkout name without inventing hits or mapping generated files."""

        payload = self._coverage_payload("/builder/work/checkout-2/Sources/File.swift")
        generated = self._coverage_payload("/builder/work/checkout-2/build/Generated.swift")
        payload["data"][0]["files"].extend(generated["data"][0]["files"])
        payload["data"][0]["functions"].extend(generated["data"][0]["functions"])
        self._replace_export(payload)
        with patch.object(AnalysisConfiguration, "project_compiler_repository_names",
                          return_value=("repository", "checkout-2")):
            result = self.analyzer().analyze(self.cycle_id, self.build_id, self.input_sha)
        self.assertEqual(result.summary["lines"]["covered"], 1)
        self.assertEqual(result.summary["lines"]["uncovered"], 1)
        details = json.loads((result.output_directory / "changed-line-coverage.json").read_text())
        self.assertEqual([line["state"] for line in details["lines"]],
                         ["covered", "uncovered", "not_measurable"])
        normalized = json.loads((result.output_directory / "normalized-coverage.json").read_text())
        self.assertEqual(normalized["unmapped_coverage_file_count"], 1)
        self.assertEqual(normalized["files"][0]["repo_relative_path"], "Sources/File.swift")
        self.assertNotIn("/builder/", json.dumps(normalized))

    def test_renamed_checkout_rejects_ambiguous_suffixes_and_roots(self) -> None:
        """Refuses multiple allowlisted splits and distinct allowlisted roots."""

        payload = self._coverage_payload("/builder/checkout-2/Modules/checkout-2/Sources/File.swift")
        with self.assertRaises(CoverageExportError) as caught:
            normalize_coverage_export(
                payload, self.repository, ("Sources/File.swift", "Modules/checkout-2/Sources/File.swift"),
                lambda _: self.controller.sources["Sources/File.swift"],
                ("checkout-2",),
            )
        self.assertEqual(caught.exception.code, "coverage_path_mapping_ambiguous")

        payload = self._coverage_payload("/builder/checkout-2/Sources/File.swift")
        other = self._coverage_payload("/vendor/another-checkout/Sources/File.swift")
        payload["data"][0]["files"].extend(other["data"][0]["files"])
        payload["data"][0]["functions"].extend(other["data"][0]["functions"])
        with self.assertRaises(CoverageExportError) as caught:
            normalize_coverage_export(
                payload, self.repository, ("Sources/File.swift",),
                lambda _: self.controller.sources["Sources/File.swift"],
                ("checkout-2", "another-checkout"),
            )
        self.assertEqual(caught.exception.code, "coverage_path_mapping_ambiguous")

    def test_renamed_checkout_requires_fixed_git_source(self) -> None:
        """Does not map a foreign root without both the Git tree and blob loader."""

        payload = self._coverage_payload("/builder/checkout-2/Sources/File.swift")
        for paths, loader in (
            (("Sources/File.swift",), None),
            (None, lambda _: self.controller.sources["Sources/File.swift"]),
        ):
            with self.subTest(paths=paths), self.assertRaises(CoverageExportError) as caught:
                normalize_coverage_export(payload, self.repository, paths, loader, ("checkout-2",))
            self.assertEqual(caught.exception.code, "coverage_path_mapping_failed")

    def test_unregistered_checkout_name_and_partial_name_are_rejected(self) -> None:
        """A matching Git suffix cannot authorize a missing or partial directory name."""

        for name in ("unregistered", "checkout-20"):
            with self.subTest(name=name), self.assertRaises(CoverageExportError) as caught:
                normalize_coverage_export(
                    self._coverage_payload("/builder/" + name + "/Sources/File.swift"),
                    self.repository, ("Sources/File.swift",),
                    lambda _: self.controller.sources["Sources/File.swift"], ("checkout-2",),
                )
            self.assertEqual(caught.exception.code, "coverage_path_mapping_failed")

    def test_compiler_prefix_is_reused_for_later_project_files(self) -> None:
        """Discovers the compiler root once while processing several files beneath it."""

        payload = self._coverage_payload("/builder/checkout-2/Sources/File.swift")
        second = self._coverage_payload("/builder/checkout-2/Sources/Other.swift")
        payload["data"][0]["files"].extend(second["data"][0]["files"])
        payload["data"][0]["functions"].extend(second["data"][0]["functions"])
        with patch.object(coverage_export, "_allowed_root_candidates",
                          wraps=coverage_export._allowed_root_candidates) as candidates:
            normalized = normalize_coverage_export(
                payload, self.repository, ("Sources/File.swift", "Sources/Other.swift"),
                lambda _: self.controller.sources["Sources/File.swift"], ("checkout-2",),
            )
        compiler_lookups = [call for call in candidates.call_args_list
                            if call.args[0].startswith("/builder/")]
        self.assertEqual(len(compiler_lookups), 1)
        self.assertEqual(set(normalized.regions_by_path), {"Sources/File.swift", "Sources/Other.swift"})

    def test_prefix_reuse_does_not_hide_nested_root_ambiguity(self) -> None:
        """Rejects a later ambiguous file even after an earlier file resolved the root."""

        payload = self._coverage_payload("/builder/checkout-2/A.swift")
        later = self._coverage_payload("/builder/checkout-2/Modules/checkout-2/Sources/File.swift")
        payload["data"][0]["files"].extend(later["data"][0]["files"])
        payload["data"][0]["functions"].extend(later["data"][0]["functions"])
        with self.assertRaises(CoverageExportError) as caught:
            normalize_coverage_export(
                payload, self.repository,
                ("A.swift", "Sources/File.swift", "Modules/checkout-2/Sources/File.swift"),
                lambda _: self.controller.sources["Sources/File.swift"], ("checkout-2",),
            )
        self.assertEqual(caught.exception.code, "coverage_path_mapping_ambiguous")

    def test_unmapped_path_inside_prefix_cannot_hide_a_second_root(self) -> None:
        """Checks exceptional paths for a second root even beneath the selected prefix."""

        payload = self._coverage_payload("/builder/checkout-2/A.swift")
        later = self._coverage_payload("/builder/checkout-2/untracked/checkout-2/Sources/File.swift")
        payload["data"][0]["files"].extend(later["data"][0]["files"])
        payload["data"][0]["functions"].extend(later["data"][0]["functions"])
        with self.assertRaises(CoverageExportError) as caught:
            normalize_coverage_export(
                payload, self.repository, ("A.swift", "Sources/File.swift"),
                lambda _: self.controller.sources["Sources/File.swift"], ("checkout-2",),
            )
        self.assertEqual(caught.exception.code, "coverage_path_mapping_ambiguous")

    def test_unmapped_segment_evidence_also_fails_without_functions(self) -> None:
        """Does not confuse real file segments with a region-free script export."""

        payload = self._coverage_payload("/builder/work/other/Sources/File.swift")
        payload["data"][0]["functions"] = []
        payload["data"][0]["files"][0]["segments"] = [[1, 1, 2, True, True, False]]
        with self.assertRaises(CoverageExportError) as caught:
            normalize_coverage_export(payload, self.repository)
        self.assertEqual(caught.exception.code, "coverage_path_mapping_failed")

    def test_ambiguous_suffix_and_mixed_foreign_roots_fail(self) -> None:
        """Does not select one of two valid suffixes or combine two compiler roots."""

        path = "/builder/work/repository/Modules/repository/Sources/File.swift"
        paths = ("Sources/File.swift", "Modules/repository/Sources/File.swift")
        with self.assertRaises(CoverageExportError) as caught:
            normalize_coverage_export(
                self._coverage_payload(path), self.repository, paths,
                lambda _: self.controller.sources["Sources/File.swift"],
            )
        self.assertEqual(caught.exception.code, "coverage_path_mapping_ambiguous")

        payload = self._coverage_payload("/builder/one/repository/Sources/File.swift")
        second = self._coverage_payload("/builder/two/repository/Sources/File.swift")
        payload["data"][0]["files"].extend(second["data"][0]["files"])
        payload["data"][0]["functions"].extend(second["data"][0]["functions"])
        with self.assertRaises(CoverageExportError) as caught:
            normalize_coverage_export(
                payload, self.repository, ("Sources/File.swift",),
                lambda _: self.controller.sources["Sources/File.swift"],
            )
        self.assertEqual(caught.exception.code, "coverage_path_mapping_ambiguous")

    def test_foreign_path_traversal_and_region_mismatch_fail(self) -> None:
        """Rejects normalized traversal and coordinates beyond the fixed Git blob."""

        for path in (
            "/builder/work/repository/Sources/../Sources/File.swift",
            "/builder/work/repository/Sources//File.swift",
            "/builder/work/checkout-2/Sources/../Sources/File.swift",
            "/builder/work/checkout-2/Sources//File.swift",
            "/vendor/repository/File.swift",
            "/vendor/checkout-2/File.swift",
        ):
            with self.subTest(path=path), self.assertRaises(CoverageExportError) as caught:
                normalize_coverage_export(
                    self._coverage_payload(path), self.repository,
                    ("Sources/File.swift",),
                    lambda _: self.controller.sources["Sources/File.swift"],
                    ("repository", "checkout-2"),
                )
            self.assertEqual(caught.exception.code, "coverage_path_mapping_failed")

        payload = self._coverage_payload("/builder/work/checkout-2/Sources/File.swift")
        payload["data"][0]["functions"][0]["regions"][0][2] = 99
        with self.assertRaises(CoverageExportError) as caught:
            normalize_coverage_export(
                payload, self.repository, ("Sources/File.swift",),
                lambda _: self.controller.sources["Sources/File.swift"],
                ("checkout-2",),
            )
        self.assertEqual(caught.exception.code, "coverage_path_source_mismatch")

    def test_foreign_coverage_segments_must_fit_git_blob(self) -> None:
        """Rejects a file record whose segment coordinates contradict fixed source."""

        payload = self._coverage_payload("/builder/work/checkout-2/Sources/File.swift")
        payload["data"][0]["files"][0]["segments"] = [[500, 1, 1, True, True, False]]
        with self.assertRaises(CoverageExportError) as caught:
            normalize_coverage_export(
                payload, self.repository, ("Sources/File.swift",),
                lambda _: self.controller.sources["Sources/File.swift"],
                ("checkout-2",),
            )
        self.assertEqual(caught.exception.code, "coverage_path_source_mismatch")

    def test_external_dependency_is_ignored_when_project_coverage_maps(self) -> None:
        """Keeps an external same-name file out of an otherwise valid report."""

        payload = self._coverage_payload("/builder/work/repository/Sources/File.swift")
        external = self._coverage_payload("/vendor/Other/Sources/File.swift")
        payload["data"][0]["files"].extend(external["data"][0]["files"])
        payload["data"][0]["functions"].extend(external["data"][0]["functions"])
        self._replace_export(payload)
        result = self.analyzer().analyze(self.cycle_id, self.build_id, self.input_sha)
        self.assertEqual(result.summary["lines"]["covered"], 1)
        self.assertEqual(result.summary["warnings"][0]["count"], 1)
        normalized = (result.output_directory / "normalized-coverage.json").read_text()
        self.assertNotIn("/vendor/", normalized)

    def test_conflicting_same_path_coverage_is_unknown(self) -> None:
        """Marks inconsistent duplicate file records instead of choosing one."""

        payload = self._coverage_payload()
        duplicate = dict(payload["data"][0]["files"][0])
        duplicate["segments"] = [[1, 1, 99, True, True, False]]
        payload["data"][0]["files"].append(duplicate)
        self._replace_export(payload)
        result = self.analyzer().analyze(self.cycle_id, self.build_id, self.input_sha)
        self.assertEqual(result.summary["lines"]["unknown"], 3)

    def test_all_positive_regions_are_covered(self) -> None:
        """Classifies a line covered only when every intersecting region is positive."""

        regions = (CoverageRegion(1, 1, 1, 5, 2, "f"), CoverageRegion(1, 2, 1, 4, 1, "g"))
        related, error = regions_for_source_line(regions, 1, ["value"])
        self.assertIsNone(error)
        self.assertTrue(all(region.count > 0 for region in related))

    def test_zero_region_is_uncovered(self) -> None:
        """Preserves a zero compiler count for conservative classification."""

        state = self.analyzer()._line_state(
            "Sources/File.swift", 2, self.controller.sources["Sources/File.swift"].splitlines(),
            normalize_coverage_export(self._coverage_payload(), self.repository),
        )
        self.assertEqual(state[0], "uncovered")

    def test_no_region_is_not_measurable(self) -> None:
        """Excludes a precisely identified comment line with no code region."""

        state = self.analyzer()._line_state(
            "Sources/File.swift", 3, self.controller.sources["Sources/File.swift"].splitlines(),
            normalize_coverage_export(self._coverage_payload(), self.repository),
        )
        self.assertEqual(state[0], "not_measurable")

    def test_out_of_range_region_is_unknown(self) -> None:
        """Does not trust coverage positions beyond the exact Git object source."""

        payload = self._coverage_payload()
        payload["data"][0]["functions"][0]["regions"][0][2] = 99
        self._replace_export(payload)
        result = self.analyzer().analyze(self.cycle_id, self.build_id, self.input_sha)
        self.assertEqual(result.summary["lines"]["unknown"], 3)

    def test_zero_length_llvm_region_is_ignored_without_poisoning_file(self) -> None:
        """Treats a valid Xcode 26.3 zero-area region as non-measuring evidence."""

        payload = self._coverage_payload()
        payload["data"][0]["functions"][0]["regions"].append([1, 5, 1, 5, 9, 0, 0, 0])
        self._replace_export(payload)
        result = self.analyzer().analyze(self.cycle_id, self.build_id, self.input_sha)
        self.assertEqual(result.summary["lines"]["covered"], 1)
        self.assertEqual(result.summary["lines"]["unknown"], 0)

    def test_mixed_positive_and_zero_regions_are_uncovered(self) -> None:
        """Applies the requested conservative mixed-region rule."""

        result = self.analyzer().analyze(self.cycle_id, self.build_id, self.input_sha)
        details = json.loads((result.output_directory / "changed-line-coverage.json").read_text())
        mixed = details["lines"][1]
        self.assertEqual(mixed["state"], "uncovered")
        self.assertIn("mixed", mixed["reason"])

    def test_zero_measurable_lines_produce_null_percent(self) -> None:
        """Avoids misleading 0 or 100 percent when the denominator is empty."""

        payload = self._coverage_payload()
        payload["data"][0]["functions"] = []
        self._replace_export(payload)
        result = self.analyzer().analyze(self.cycle_id, self.build_id, self.input_sha)
        self.assertIsNone(result.summary["changed_line_coverage_percent"])

    def test_nonmeasurable_unknown_and_deleted_do_not_enter_denominator(self) -> None:
        """Uses covered plus uncovered as the only measurable denominator."""

        result = self.analyzer().analyze(self.cycle_id, self.build_id, self.input_sha)
        lines = result.summary["lines"]
        self.assertEqual(lines["measurable"], lines["covered"] + lines["uncovered"])
        self.assertNotEqual(lines["changed_new_side"] + lines["deleted_excluded"], lines["measurable"])

    def test_git_diff_uses_only_fixed_merge_base_and_build_head(self) -> None:
        """Never reads master/origin or Cycle base to build the report denominator."""

        self.analyzer().analyze(self.cycle_id, self.build_id, self.input_sha)
        records = self.controller.runners[-1].records
        diff_args = next(record["arguments"] for record in records if record["label"] == "git-diff")
        self.assertIn(self.merge_base, diff_args)
        self.assertIn(self.git_sha, diff_args)
        self.assertFalse(any("master" in value for value in diff_args))

    def test_target_branch_movement_cannot_change_fixed_identity(self) -> None:
        """Reuses the same result because no mutable branch ref is an input."""

        first = self.analyzer().analyze(self.cycle_id, self.build_id, self.input_sha)
        second = self.analyzer().analyze(self.cycle_id, self.build_id, self.input_sha)
        self.assertEqual(first.diff_set_sha256, second.diff_set_sha256)

    def test_build_cycle_or_single_result_identity_mismatch_is_rejected(self) -> None:
        """Rejects both SQLite ownership and prior-result identity drift."""

        with self.assertRaises(SingleBuildDiffAnalysisError) as context:
            self.analyzer().analyze(str(uuid.uuid4()), self.build_id, self.input_sha)
        self.assertEqual(context.exception.code, "cycle_not_found")
        summary_path = self._single_result_dir() / "summary.json"
        summary = json.loads(summary_path.read_text())
        summary["git_sha"] = "9" * 40
        self._rewrite_single_output(summary_path, summary)
        with self.assertRaises(SingleBuildDiffAnalysisError) as context:
            self.analyzer().analyze(self.cycle_id, self.build_id, self.input_sha)
        self.assertEqual(context.exception.code, "single_build_identity_mismatch")

    def test_tampered_completion_or_export_is_rejected(self) -> None:
        """Never consumes an incomplete or byte-modified prior coverage result."""

        export_path = self._single_result_dir() / "llvm-cov-export.json"
        export_path.write_text("{}", encoding="utf-8")
        with self.assertRaises(SingleBuildDiffAnalysisError) as context:
            self.analyzer().analyze(self.cycle_id, self.build_id, self.input_sha)
        self.assertEqual(context.exception.code, "single_build_result_tampered")

    def test_head_and_dirty_worktree_do_not_change_object_addressed_result(self) -> None:
        """Uses fixed Git object source even when checkout state and disk bytes differ."""

        self.controller.head = "9" * 40
        self.controller.dirty = True
        dirty_source = self.repository / "Sources" / "File.swift"
        dirty_source.parent.mkdir(parents=True)
        dirty_source.write_text("working tree must not be read\n", encoding="utf-8")
        result = self.analyzer().analyze(self.cycle_id, self.build_id, self.input_sha)
        lines = json.loads(
            (result.output_directory / "changed-line-coverage.json").read_text()
        )["lines"]
        self.assertEqual(lines[0]["source_text"], "let coveredValue = 1")
        labels = self.controller.runners[-1].labels
        self.assertNotIn("git-head", labels)
        self.assertNotIn("git-worktree-status", labels)

    def test_wrong_fixed_repository_root_is_rejected(self) -> None:
        """Requires the configured repository itself to be the Git worktree root."""

        self.controller.repository_root = str(self.root / "different-repository")
        with self.assertRaises(SingleBuildDiffAnalysisError) as context:
            self.analyzer().analyze(self.cycle_id, self.build_id, self.input_sha)
        self.assertEqual(context.exception.code, "git_repository_invalid")

    def test_git_failure_or_stderr_is_fatal(self) -> None:
        """Rejects every Git nonzero exit and every nonempty stderr."""

        self.controller.failure_label = "git-name-status"
        with self.assertRaises(SingleBuildDiffAnalysisError) as context:
            self.analyzer().analyze(self.cycle_id, self.build_id, self.input_sha)
        self.assertEqual(context.exception.code, "git_command_failed")
        self.controller.failure_label = None
        self.controller.stderr_label = "git-diff"
        with self.assertRaises(SingleBuildDiffAnalysisError) as context:
            self.analyzer().analyze(self.cycle_id, self.build_id, self.input_sha)
        self.assertEqual(context.exception.code, "git_stderr_not_empty")

    def test_failed_staging_does_not_publish_success(self) -> None:
        """Moves failure evidence outside the immutable success namespace."""

        self.controller.failure_label = "git-name-status"
        with self.assertRaises(SingleBuildDiffAnalysisError):
            self.analyzer().analyze(self.cycle_id, self.build_id, self.input_sha)
        final_parent = self.analysis_root.parent / "single-build-diff" / self.build_id / self.input_sha
        self.assertFalse(final_parent.exists())
        failures = list((self.analysis_root.parent / "single-build-diff" / ".failed").rglob("failure.json"))
        self.assertTrue(failures)

    def test_identical_input_reuses_complete_result(self) -> None:
        """Returns the verified same hash and conclusion on replay."""

        first = self.analyzer().analyze(self.cycle_id, self.build_id, self.input_sha)
        second = self.analyzer().analyze(self.cycle_id, self.build_id, self.input_sha)
        self.assertFalse(first.idempotent_replay)
        self.assertTrue(second.idempotent_replay)
        self.assertEqual(first.diff_set_sha256, second.diff_set_sha256)
        self.assertEqual(first.summary, second.summary)

    def test_semantic_diff_or_export_change_changes_diff_set_hash(self) -> None:
        """Makes both normalized diff and verified export bytes part of result identity."""

        first = self.analyzer().analyze(self.cycle_id, self.build_id, self.input_sha)
        shutil_target = first.output_directory
        # A second isolated input-set identity avoids overwriting the first immutable result.
        self.input_sha = "e" * 64
        payload = self._coverage_payload()
        payload["data"][0]["functions"][0]["regions"][0][4] = 7
        self._create_single_build_result(payload)
        second = self.analyzer().analyze(self.cycle_id, self.build_id, self.input_sha)
        self.assertNotEqual(first.diff_set_sha256, second.diff_set_sha256)
        self.input_sha = "d" * 64
        self._create_single_build_result(payload)
        self.controller.patch = self.controller.patch.replace(
            "@@ -1 +1,3 @@", "@@ -1 +1,4 @@"
        ) + "+let newDiffLine = true\n"
        self.controller.sources["Sources/File.swift"] += "let newDiffLine = true\n"
        third = self.analyzer().analyze(self.cycle_id, self.build_id, self.input_sha)
        self.assertNotEqual(second.diff_set_sha256, third.diff_set_sha256)
        self.assertTrue(shutil_target.is_dir())

    def _coverage_payload(self, filename: Optional[str] = None) -> Dict[str, object]:
        """Creates three changed lines: positive, mixed, and unmapped."""

        source = filename or str(self.repository / "Sources" / "File.swift")
        return {
            "type": "llvm.coverage.json.export",
            "version": "2.0.1",
            "data": [
                {
                    "files": [{"filename": source, "segments": [], "summary": {}}],
                    "functions": [
                        {
                            "name": "covered",
                            "filenames": [source],
                            "regions": [[1, 1, 1, 21, 2, 0, 0, 0]],
                        },
                        {
                            "name": "mixed-positive",
                            "filenames": [source],
                            "regions": [[2, 1, 2, 35, 2, 0, 0, 0]],
                        },
                        {
                            "name": "mixed-zero",
                            "filenames": [source],
                            "regions": [[2, 5, 2, 20, 0, 0, 0, 0]],
                        },
                    ],
                    "totals": {},
                }
            ],
        }

    def _create_database_and_build(self) -> None:
        """Persists only production columns consumed by the diff analyzer."""

        self.database.parent.mkdir(parents=True)
        main_sha = hashlib.sha256(self.main_bytes).hexdigest()
        dsym_sha = hashlib.sha256(self.dsym_bytes).hexdigest()
        manifest = {
            "schema_version": 1,
            "build_id": self.build_id,
            "cycle_id": self.cycle_id,
            "git_sha": self.git_sha,
            "target_sha_at_build": self.target_sha,
            "merge_base_sha": self.merge_base,
            "coverage_binary": "main_executable",
            "macho_uuids": {"main_executable": [self.macho_uuid], "dsym": [self.macho_uuid]},
            "artifacts": {
                "main_executable": {
                    "filename": "ExampleApp",
                    "size": len(self.main_bytes),
                    "sha256": main_sha,
                },
                "dsym": {
                    "filename": "ExampleApp.app.dSYM.zip",
                    "archive_format": "zip",
                    "size": len(self.dsym_bytes),
                    "sha256": dsym_sha,
                },
            },
        }
        canonical = self._canonical(manifest)
        build_dir = self.data_dir / "cycles" / self.cycle_id / "builds" / self.build_id
        artifacts = build_dir / "artifacts"
        artifacts.mkdir(parents=True)
        (build_dir / "manifest.json").write_bytes(canonical + b"\n")
        (artifacts / "main_executable.bin").write_bytes(self.main_bytes)
        (artifacts / "dsym.zip").write_bytes(self.dsym_bytes)
        with sqlite3.connect(str(self.database)) as connection:
            connection.executescript(
                """
                CREATE TABLE coverage_cycles (id TEXT PRIMARY KEY);
                CREATE TABLE builds (
                    id TEXT PRIMARY KEY, cycle_id TEXT, git_sha TEXT,
                    target_sha_at_build TEXT, merge_base_sha TEXT,
                    coverage_binary TEXT, artifact_state TEXT,
                    manifest_sha256 TEXT, manifest_json TEXT,
                    main_executable_sha256 TEXT, main_executable_size INTEGER,
                    debug_dylib_sha256 TEXT, debug_dylib_size INTEGER,
                    dsym_sha256 TEXT, dsym_size INTEGER,
                    main_executable_uuids_json TEXT,
                    debug_dylib_uuids_json TEXT, dsym_uuids_json TEXT
                );
                """
            )
            connection.execute("INSERT INTO coverage_cycles VALUES (?)", (self.cycle_id,))
            connection.execute(
                "INSERT INTO builds VALUES (?, ?, ?, ?, ?, 'main_executable', 'ready', ?, ?, ?, ?, NULL, NULL, ?, ?, ?, NULL, ?)",
                (
                    self.build_id,
                    self.cycle_id,
                    self.git_sha,
                    self.target_sha,
                    self.merge_base,
                    hashlib.sha256(canonical).hexdigest(),
                    canonical.decode("utf-8"),
                    main_sha,
                    len(self.main_bytes),
                    dsym_sha,
                    len(self.dsym_bytes),
                    json.dumps([self.macho_uuid]),
                    json.dumps([self.macho_uuid]),
                ),
            )

    def _create_single_build_result(self, export_payload: Dict[str, object]) -> None:
        """Creates one completion-protected prior single-Build analysis result."""

        directory = self._single_result_dir()
        directory.mkdir(parents=True, exist_ok=True)
        inputs = {
            "schema_version": 1,
            "cycle_id": self.cycle_id,
            "build_id": self.build_id,
            "input_set_sha256": self.input_sha,
            "build": {
                "git_sha": self.git_sha,
                "target_sha_at_build": self.target_sha,
                "merge_base_sha": self.merge_base,
                "manifest_sha256": self._manifest_sha(),
                "coverage_binary": "main_executable",
                "actual_macho_uuids": {
                    "main_executable": [self.macho_uuid],
                    "dsym": [self.macho_uuid],
                },
                "manifest": self._manifest(),
            },
            "runs": [],
        }
        summary = {
            "schema_version": 1,
            "status": "complete",
            "cycle_id": self.cycle_id,
            "build_id": self.build_id,
            "input_set_sha256": self.input_sha,
            "git_sha": self.git_sha,
            "target_sha_at_build": self.target_sha,
            "merge_base_sha": self.merge_base,
            "manifest_sha256": self._manifest_sha(),
            "run_ids": [],
        }
        for name, value in (
            ("inputs.json", inputs),
            ("summary.json", summary),
            ("llvm-cov-export.json", export_payload),
        ):
            (directory / name).write_text(json.dumps(value, sort_keys=True), encoding="utf-8")
        (directory / "merged.profdata").write_bytes(b"merged-profile")
        outputs = self._output_identities(directory)
        completion = {
            "schema_version": 1,
            "status": "complete",
            "input_set_sha256": self.input_sha,
            "outputs": outputs,
        }
        (directory / "completion.json").write_text(json.dumps(completion, sort_keys=True), encoding="utf-8")

    def _replace_export(self, payload: Dict[str, object]) -> None:
        """Replaces a fixture export and refreshes its prior completion identity."""

        path = self._single_result_dir() / "llvm-cov-export.json"
        self._rewrite_single_output(path, payload)

    def _rewrite_single_output(self, path: Path, payload: Dict[str, object]) -> None:
        """Changes one fixture output while retaining a valid completion manifest."""

        path.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
        completion_path = self._single_result_dir() / "completion.json"
        completion = json.loads(completion_path.read_text())
        completion["outputs"] = self._output_identities(self._single_result_dir())
        completion_path.write_text(json.dumps(completion, sort_keys=True), encoding="utf-8")

    def _single_result_dir(self) -> Path:
        """Returns the current fixture input-set directory."""

        return self.analysis_root / self.build_id / self.input_sha

    def _manifest_sha(self) -> str:
        """Returns the immutable Build manifest identity from SQLite."""

        with sqlite3.connect(str(self.database)) as connection:
            return str(connection.execute("SELECT manifest_sha256 FROM builds").fetchone()[0])

    def _manifest(self) -> Dict[str, object]:
        """Returns the stored immutable Build manifest used by retained evidence."""

        with sqlite3.connect(str(self.database)) as connection:
            encoded = str(connection.execute("SELECT manifest_json FROM builds").fetchone()[0])
        return json.loads(encoded)

    def _output_identities(self, directory: Path) -> Dict[str, Dict[str, object]]:
        """Hashes every prior result file except completion.json."""

        result = {}
        for path in directory.iterdir():
            if path.is_file() and path.name != "completion.json":
                data = path.read_bytes()
                result[path.name] = {"size": len(data), "sha256": hashlib.sha256(data).hexdigest()}
        return result

    def _canonical(self, payload: object) -> bytes:
        """Matches production semantic JSON hashing."""

        return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")


if __name__ == "__main__":
    unittest.main()
