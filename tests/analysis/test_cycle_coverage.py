#!/usr/bin/env python3
"""Focused tests for conservative cross-Build source-region accumulation."""

import hashlib
import json
import sqlite3
import subprocess
import tempfile
import unittest
import uuid
from pathlib import Path
from typing import Dict, List, Mapping, Optional, Sequence, Tuple

from ioscoverage.analysis.command_runner import EvidenceCommandRunner
from ioscoverage.cli.analyze_cycle_coverage import parse_build_inputs
from ioscoverage.analysis.cumulative.analyzer import CycleCoverageAnalysisError, CycleCoverageAnalyzer
from ioscoverage.analysis.single_build.analyzer import AnalysisConfiguration


Region = Tuple[str, int, int, int, int, int, str]


class FaultInjectingRunner(EvidenceCommandRunner):
    """Runs real Git while injecting one selected return-code or stderr failure."""

    def __init__(self, evidence_dir: Path, developer_dir: Path, label: str, stderr: bool):
        """Stores the one command label whose returned result will be altered."""

        super().__init__(evidence_dir, developer_dir)
        self.label = label
        self.inject_stderr = stderr

    def run(self, label, arguments, stdout_filename=None, timeout_seconds=300):
        """Delegates evidence creation and changes only the selected result."""

        result = super().run(label, arguments, stdout_filename, timeout_seconds)
        if label != self.label:
            return result
        return subprocess.CompletedProcess(
            result.args,
            0 if self.inject_stderr else 1,
            stdout=result.stdout,
            stderr="injected warning\n" if self.inject_stderr else result.stderr,
        )


class FailingPublishAnalyzer(CycleCoverageAnalyzer):
    """Injects a deterministic write failure before a success marker exists."""

    def _write_json(self, path: Path, payload: object) -> None:
        """Fails when publishing summary while leaving failure handling available."""

        if path.name == "summary.json":
            raise OSError("injected staging failure")
        super()._write_json(path, payload)


class CycleCoverageAnalyzerTests(unittest.TestCase):
    """Uses real temporary Git objects and synthetic immutable LLVM exports."""

    def setUp(self) -> None:
        """Creates isolated installation roots and one initialized Git repository."""

        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.repository = self.root / "repository"
        self.database = self.root / "instance" / "coverage.sqlite3"
        self.data_dir = self.root / "data"
        self.analysis_root = self.root / "analysis" / "single-build"
        self.cycle_id = str(uuid.uuid4())
        self.other_cycle_id = str(uuid.uuid4())
        self.build_a = str(uuid.uuid4())
        self.build_b = str(uuid.uuid4())
        self.build_c = str(uuid.uuid4())
        self.input_a = "a" * 64
        self.input_b = "b" * 64
        self.input_c = "c" * 64
        self.configuration = AnalysisConfiguration(
            database_path=self.database,
            data_dir=self.data_dir,
            analysis_root=self.analysis_root,
            repository_path=self.repository,
        )
        self._git("init", "-q", str(self.repository))
        self._git("-C", str(self.repository), "config", "user.name", "Coverage Test")
        self._git("-C", str(self.repository), "config", "user.email", "coverage@example.test")
        self._write_files({"Sources/File.swift": "// base\n"})
        self.base_sha = self._commit("base")
        self._create_database()

    def tearDown(self) -> None:
        """Removes every isolated Git, SQLite, artifact, and analysis fixture."""

        self.temporary.cleanup()

    def test_two_round_accumulation_inherits_x_and_uses_current_y(self) -> None:
        """Proves X from A plus modified-and-hit Y from B yields both covered."""

        source_a = "// base\nlet x = 1\nlet y = 1\n"
        source_b = "// base\nlet x = 1\nlet y = 2\n"
        sha_a = self._commit_source("Sources/File.swift", source_a, "build A")
        sha_b = self._commit_source("Sources/File.swift", source_b, "build B")
        self._register_build(self.build_a, sha_a, 1)
        self._register_build(self.build_b, sha_b, 2)
        self._single_result(
            self.build_a,
            self.input_a,
            sha_a,
            self._coverage(source_a, [("Sources/File.swift", 2, 1, 2, 10, 1, "x"), ("Sources/File.swift", 3, 1, 3, 10, 0, "y")]),
        )
        self._single_result(
            self.build_b,
            self.input_b,
            sha_b,
            self._coverage(source_b, [("Sources/File.swift", 2, 1, 2, 10, 0, "x"), ("Sources/File.swift", 3, 1, 3, 10, 1, "y")]),
        )

        result = self._analyze(self.build_b, {self.build_a: self.input_a, self.build_b: self.input_b})

        details = self._lines(result.output_directory)
        self.assertEqual({line["new_line_number"]: line["state"] for line in details}, {2: "covered", 3: "covered"})
        inherited = details[0]["regions"][0]["source"]
        self.assertEqual(inherited["kind"], "inherited_build")
        self.assertEqual(inherited["build_id"], self.build_a)
        self.assertEqual(details[1]["regions"][0]["source"]["kind"], "current_build")

    def test_modified_x_invalidates_historical_hit(self) -> None:
        """Rejects A's X hit after B changes X and leaves its current count zero."""

        source_a = "// base\nlet x = 1\n"
        source_b = "// base\nlet x = 2\n"
        sha_a = self._commit_source("Sources/File.swift", source_a, "build A")
        sha_b = self._commit_source("Sources/File.swift", source_b, "build B")
        self._register_build(self.build_a, sha_a, 1)
        self._register_build(self.build_b, sha_b, 2)
        self._single_result(self.build_a, self.input_a, sha_a, self._coverage(source_a, [("Sources/File.swift", 2, 1, 2, 10, 1, "x")]))
        self._single_result(self.build_b, self.input_b, sha_b, self._coverage(source_b, [("Sources/File.swift", 2, 1, 2, 10, 0, "x")]))

        result = self._analyze(self.build_b, {self.build_a: self.input_a, self.build_b: self.input_b})

        line = self._lines(result.output_directory)[0]
        self.assertEqual(line["state"], "uncovered")
        self.assertEqual(
            line["regions"][0]["inheritance_attempts"][0]["reason"],
            "region_contains_modified_or_added_line",
        )

    def test_current_positive_region_wins_without_history_downgrade(self) -> None:
        """Uses current positive evidence even when the historical count is zero."""

        source = "// base\nlet x = 1\n"
        sha_a = self._commit_source("Sources/File.swift", source, "build A")
        sha_b = self._commit("build B", allow_empty=True)
        self._register_build(self.build_a, sha_a, 1)
        self._register_build(self.build_b, sha_b, 2)
        self._single_result(self.build_a, self.input_a, sha_a, self._coverage(source, [("Sources/File.swift", 2, 1, 2, 10, 0, "x")]))
        self._single_result(self.build_b, self.input_b, sha_b, self._coverage(source, [("Sources/File.swift", 2, 1, 2, 10, 1, "x")]))

        line = self._lines(self._analyze(self.build_b, {self.build_a: self.input_a, self.build_b: self.input_b}).output_directory)[0]

        self.assertEqual(line["state"], "covered")
        self.assertEqual(line["regions"][0]["source"]["kind"], "current_build")
        self.assertEqual(line["regions"][0]["inheritance_attempts"], [])

    def test_new_region_cannot_inherit_same_text_from_elsewhere(self) -> None:
        """Does not reuse identical bytes from a different repository identity."""

        source = "let x = 1\n"
        self._write_files({"Old/File.swift": source})
        sha_a = self._commit("build A")
        self._write_files({"New/File.swift": source})
        sha_b = self._commit("build B")
        self._register_build(self.build_a, sha_a, 1)
        self._register_build(self.build_b, sha_b, 2)
        self._single_result(self.build_a, self.input_a, sha_a, self._coverage(source, [("Old/File.swift", 1, 1, 1, 10, 1, "x")]))
        self._single_result(self.build_b, self.input_b, sha_b, self._coverage(source, [("New/File.swift", 1, 1, 1, 10, 0, "x")]))

        lines = self._lines(self._analyze(self.build_b, {self.build_a: self.input_a, self.build_b: self.input_b}).output_directory)
        new_line = next(line for line in lines if line["repo_relative_path"] == "New/File.swift")

        self.assertEqual(new_line["state"], "uncovered")
        self.assertEqual(new_line["regions"][0]["inheritance_attempts"][0]["reason"], "file_added_after_historical_build")

    def test_line_shift_preserves_exact_region_identity(self) -> None:
        """Maps an unchanged region after a preceding insertion moves its line."""

        source_a = "// base\nlet x = 1\n"
        source_b = "// base\n// inserted\nlet x = 1\n"
        sha_a = self._commit_source("Sources/File.swift", source_a, "build A")
        sha_b = self._commit_source("Sources/File.swift", source_b, "build B")
        self._register_build(self.build_a, sha_a, 1)
        self._register_build(self.build_b, sha_b, 2)
        self._single_result(self.build_a, self.input_a, sha_a, self._coverage(source_a, [("Sources/File.swift", 2, 1, 2, 10, 1, "x")]))
        self._single_result(self.build_b, self.input_b, sha_b, self._coverage(source_b, [("Sources/File.swift", 3, 1, 3, 10, 0, "x")]))

        lines = self._lines(self._analyze(self.build_b, {self.build_a: self.input_a, self.build_b: self.input_b}).output_directory)
        x_line = next(line for line in lines if line["new_line_number"] == 3)

        self.assertEqual(x_line["state"], "covered")
        self.assertEqual(x_line["regions"][0]["source"]["historical_region_range"]["start_line"], 2)

    def test_explicit_git_rename_preserves_region_identity(self) -> None:
        """Allows an exact Git-proven rename without basename guessing."""

        source = "let x = 1\n"
        self._write_files({"Old.swift": source})
        sha_a = self._commit("build A")
        (self.repository / "Old.swift").rename(self.repository / "New.swift")
        sha_b = self._commit("build B")
        self._register_build(self.build_a, sha_a, 1)
        self._register_build(self.build_b, sha_b, 2)
        self._single_result(self.build_a, self.input_a, sha_a, self._coverage(source, [("Old.swift", 1, 1, 1, 10, 1, "Old.swift:x")]))
        self._single_result(self.build_b, self.input_b, sha_b, self._coverage(source, [("New.swift", 1, 1, 1, 10, 0, "New.swift:x")]))

        result = self._analyze(self.build_b, {self.build_a: self.input_a, self.build_b: self.input_b})
        evidence = json.loads((result.output_directory / "mapping-evidence.json").read_text())

        mapping = evidence["region_mappings"][0]
        self.assertTrue(mapping["covered"])
        self.assertEqual(mapping["source"]["historical_path"], "Old.swift")

    def test_multiline_region_is_invalidated_when_one_line_changes(self) -> None:
        """Invalidates a whole historical region if any mapped source line changed."""

        source_a = "// base\nlet x = foo(\n  1)\n"
        source_b = "// base\nlet x = foo(\n  2)\n"
        sha_a = self._commit_source("Sources/File.swift", source_a, "build A")
        sha_b = self._commit_source("Sources/File.swift", source_b, "build B")
        self._register_build(self.build_a, sha_a, 1)
        self._register_build(self.build_b, sha_b, 2)
        region_a = [("Sources/File.swift", 2, 1, 3, 5, 1, "x")]
        region_b = [("Sources/File.swift", 2, 1, 3, 5, 0, "x")]
        self._single_result(self.build_a, self.input_a, sha_a, self._coverage(source_a, region_a))
        self._single_result(self.build_b, self.input_b, sha_b, self._coverage(source_b, region_b))

        lines = self._lines(self._analyze(self.build_b, {self.build_a: self.input_a, self.build_b: self.input_b}).output_directory)

        self.assertTrue(all(line["state"] == "uncovered" for line in lines if line["new_line_number"] in (2, 3)))

    def test_multiple_histories_can_cover_distinct_regions_on_one_line(self) -> None:
        """Requires every current region while allowing distinct Builds as sources."""

        source = "// base\nlet value = left() + right()\n"
        sha_a = self._commit_source("Sources/File.swift", source, "build A")
        sha_c = self._commit("build C", allow_empty=True)
        sha_b = self._commit("build B", allow_empty=True)
        self._register_build(self.build_a, sha_a, 1)
        self._register_build(self.build_c, sha_c, 2)
        self._register_build(self.build_b, sha_b, 3)
        regions_a = [("Sources/File.swift", 2, 13, 2, 19, 1, "left"), ("Sources/File.swift", 2, 22, 2, 29, 0, "right")]
        regions_c = [("Sources/File.swift", 2, 13, 2, 19, 0, "left"), ("Sources/File.swift", 2, 22, 2, 29, 1, "right")]
        regions_b = [("Sources/File.swift", 2, 13, 2, 19, 0, "left"), ("Sources/File.swift", 2, 22, 2, 29, 0, "right")]
        self._single_result(self.build_a, self.input_a, sha_a, self._coverage(source, regions_a))
        self._single_result(self.build_c, self.input_c, sha_c, self._coverage(source, regions_c))
        self._single_result(self.build_b, self.input_b, sha_b, self._coverage(source, regions_b))

        line = self._lines(self._analyze(self.build_b, {self.build_a: self.input_a, self.build_c: self.input_c, self.build_b: self.input_b}).output_directory)[0]

        self.assertEqual(line["state"], "covered")
        self.assertEqual({region["source"]["build_id"] for region in line["regions"]}, {self.build_a, self.build_c})

    def test_zero_region_remaining_keeps_mixed_line_uncovered(self) -> None:
        """Keeps one line uncovered when any region lacks compatible hit evidence."""

        source = "// base\nlet value = left() + right()\n"
        sha_a = self._commit_source("Sources/File.swift", source, "build A")
        sha_b = self._commit("build B", allow_empty=True)
        self._register_build(self.build_a, sha_a, 1)
        self._register_build(self.build_b, sha_b, 2)
        regions = [("Sources/File.swift", 2, 13, 2, 19, 1, "left"), ("Sources/File.swift", 2, 22, 2, 29, 0, "right")]
        current = [(path, sl, sc, el, ec, 0, name) for path, sl, sc, el, ec, _, name in regions]
        self._single_result(self.build_a, self.input_a, sha_a, self._coverage(source, regions))
        self._single_result(self.build_b, self.input_b, sha_b, self._coverage(source, current))

        line = self._lines(self._analyze(self.build_b, {self.build_a: self.input_a, self.build_b: self.input_b}).output_directory)[0]

        self.assertEqual(line["state"], "uncovered")
        self.assertEqual([region["covered"] for region in line["regions"]], [True, False])

    def test_not_measurable_unknown_and_deleted_are_outside_denominator(self) -> None:
        """Counts only covered plus uncovered and excludes deleted lines."""

        current_source = "let x = 1\n// note\n"
        self._write_files({"Sources/File.swift": current_source})
        sha_current = self._commit("build B with a deleted base line")
        self._register_build(self.build_b, sha_current, 2)
        coverage = self._coverage(current_source, [("Sources/File.swift", 1, 1, 1, 10, 1, "x")])
        absolute = str(self.repository / "Sources" / "File.swift")
        coverage["data"][0]["files"].append({"filename": absolute, "segments": [[1]], "summary": {}})
        self._single_result(self.build_b, self.input_b, sha_current, coverage)

        result = self._analyze(self.build_b, {self.build_b: self.input_b})

        self.assertEqual(result.summary["lines"]["measurable"], 0)
        self.assertGreaterEqual(result.summary["lines"]["unknown"], 1)
        self.assertGreaterEqual(result.summary["lines"]["deleted_excluded"], 1)
        self.assertIsNone(result.summary["changed_line_coverage_percent"])

    def test_missing_current_duplicate_future_and_other_cycle_are_rejected(self) -> None:
        """Rejects invalid Build selection before cumulative publication."""

        source = "// base\nlet x = 1\n"
        sha_a = self._commit_source("Sources/File.swift", source, "build A")
        sha_b = self._commit("build B", allow_empty=True)
        self._register_build(self.build_a, sha_a, 1)
        self._register_build(self.build_b, sha_b, 2)
        self._register_build(self.build_c, sha_a, 3)
        payload = self._coverage(source, [("Sources/File.swift", 2, 1, 2, 10, 1, "x")])
        self._single_result(self.build_a, self.input_a, sha_a, payload)
        self._single_result(self.build_b, self.input_b, sha_b, payload)
        self._single_result(self.build_c, self.input_c, sha_a, payload)
        with self.assertRaisesRegex(CycleCoverageAnalysisError, "current_build_id"):
            self._analyze(self.build_b, {self.build_a: self.input_a})
        with self.assertRaisesRegex(CycleCoverageAnalysisError, "created after"):
            self._analyze(self.build_b, {self.build_b: self.input_b, self.build_c: self.input_c})
        with self.assertRaises(CycleCoverageAnalysisError) as context:
            self._analyze(self.build_b, {self.build_b: self.input_b}, cycle_id=self.other_cycle_id)
        self.assertEqual(context.exception.code, "build_cycle_mismatch")
        with self.assertRaises(CycleCoverageAnalysisError) as duplicate:
            parse_build_inputs([self.build_a + "=" + self.input_a, self.build_a + "=" + self.input_b])
        self.assertEqual(duplicate.exception.code, "duplicate_build_input")

    def test_incomplete_or_tampered_single_build_result_is_rejected(self) -> None:
        """Never consumes missing completion or modified LLVM export bytes."""

        source = "// base\nlet x = 1\n"
        sha_b = self._commit_source("Sources/File.swift", source, "build B")
        self._register_build(self.build_b, sha_b, 2)
        self._single_result(self.build_b, self.input_b, sha_b, self._coverage(source, [("Sources/File.swift", 2, 1, 2, 10, 1, "x")]))
        result_dir = self._single_directory(self.build_b, self.input_b)
        (result_dir / "llvm-cov-export.json").write_text("{}", encoding="utf-8")

        with self.assertRaises(CycleCoverageAnalysisError) as context:
            self._analyze(self.build_b, {self.build_b: self.input_b})

        self.assertEqual(context.exception.code, "single_build_result_tampered")
        self.assertFalse(any((self.analysis_root.parent / "cycle-coverage").glob("*/*/*/complete.json")))

    def test_missing_single_build_completion_is_rejected(self) -> None:
        """Rejects an otherwise present single-Build directory without completion proof."""

        source = "// base\nlet x = 1\n"
        sha_b = self._commit_source("Sources/File.swift", source, "build B")
        self._register_build(self.build_b, sha_b, 2)
        self._single_result(self.build_b, self.input_b, sha_b, self._coverage(source, []))
        (self._single_directory(self.build_b, self.input_b) / "completion.json").unlink()

        with self.assertRaises(CycleCoverageAnalysisError) as context:
            self._analyze(self.build_b, {self.build_b: self.input_b})

        self.assertEqual(context.exception.code, "single_build_result_invalid")

    def test_same_input_is_idempotent_and_preserves_inputs(self) -> None:
        """Reuses a validated result without changing SQLite, artifacts, or prior analysis."""

        source = "// base\nlet x = 1\n"
        sha_b = self._commit_source("Sources/File.swift", source, "build B")
        self._register_build(self.build_b, sha_b, 2)
        self._single_result(self.build_b, self.input_b, sha_b, self._coverage(source, [("Sources/File.swift", 2, 1, 2, 10, 1, "x")]))
        before = self._tree_identity([self.database, self.data_dir, self.analysis_root])

        first = self._analyze(self.build_b, {self.build_b: self.input_b})
        second = self._analyze(self.build_b, {self.build_b: self.input_b})

        self.assertFalse(first.idempotent_replay)
        self.assertTrue(second.idempotent_replay)
        self.assertEqual(first.cycle_input_set_sha256, second.cycle_input_set_sha256)
        self.assertEqual(first.summary["lines"], second.summary["lines"])
        self.assertEqual(before, self._tree_identity([self.database, self.data_dir, self.analysis_root]))

    def test_fixed_sha_commands_do_not_read_mutable_target_branches(self) -> None:
        """Uses only persisted merge-base/head objects for the denominator."""

        source = "// base\nlet x = 1\n"
        sha_b = self._commit_source("Sources/File.swift", source, "build B")
        self._register_build(self.build_b, sha_b, 2)
        self._single_result(self.build_b, self.input_b, sha_b, self._coverage(source, [("Sources/File.swift", 2, 1, 2, 10, 1, "x")]))

        result = self._analyze(self.build_b, {self.build_b: self.input_b})
        commands = json.loads((result.output_directory / "commands.json").read_text())["commands"]
        joined = "\n".join(" ".join(command["arguments"]) for command in commands)

        self.assertIn(self.base_sha, joined)
        self.assertIn(sha_b, joined)
        self.assertNotIn("origin/master", joined)
        self.assertNotIn(" master", joined)

    def test_not_measurable_lines_are_excluded_from_percent(self) -> None:
        """Leaves a compiler-unmapped comment outside the measurable denominator."""

        source = "// base\nlet x = 1\n// changed note\n"
        sha_b = self._commit_source("Sources/File.swift", source, "build B")
        self._register_build(self.build_b, sha_b, 2)
        self._single_result(
            self.build_b,
            self.input_b,
            sha_b,
            self._coverage(source, [("Sources/File.swift", 2, 1, 2, 10, 1, "x")]),
        )

        result = self._analyze(self.build_b, {self.build_b: self.input_b})

        self.assertEqual(result.summary["lines"]["covered"], 1)
        self.assertEqual(result.summary["lines"]["not_measurable"], 1)
        self.assertEqual(result.summary["changed_line_coverage_percent"], 100.0)

    def test_head_and_dirty_worktree_do_not_change_fixed_object_result(self) -> None:
        """Ignores checkout state and same-path disk edits without modifying either."""

        source = "// base\nlet x = 1\n"
        sha_b = self._commit_source("Sources/File.swift", source, "build B")
        self._register_build(self.build_b, sha_b, 2)
        self._single_result(
            self.build_b,
            self.input_b,
            sha_b,
            self._coverage(source, [("Sources/File.swift", 2, 1, 2, 10, 1, "x")]),
        )
        clean = self._analyze(self.build_b, {self.build_b: self.input_b})
        self._commit("different current HEAD", allow_empty=True)
        tracked = self.repository / "Sources" / "File.swift"
        tracked.write_text("working tree must not be read\n", encoding="utf-8")
        dirty = self.repository / "dirty.txt"
        dirty.write_text("keep me", encoding="utf-8")

        replay = self._analyze(self.build_b, {self.build_b: self.input_b})

        self.assertEqual(replay.cycle_input_set_sha256, clean.cycle_input_set_sha256)
        self.assertEqual(replay.summary, clean.summary)
        self.assertEqual(tracked.read_text(encoding="utf-8"), "working tree must not be read\n")
        self.assertEqual(dirty.read_text(encoding="utf-8"), "keep me")

    def test_git_nonzero_and_stderr_are_fatal(self) -> None:
        """Treats every Git nonzero exit and every warning stream as analysis failure."""

        source = "// base\nlet x = 1\n"
        sha_b = self._commit_source("Sources/File.swift", source, "build B")
        self._register_build(self.build_b, sha_b, 2)
        self._single_result(self.build_b, self.input_b, sha_b, self._coverage(source, []))
        for stderr, expected in ((False, "git_command_failed"), (True, "git_stderr_not_empty")):
            with self.subTest(stderr=stderr):
                factory = lambda evidence, developer, stderr=stderr: FaultInjectingRunner(
                    evidence, developer, "git-repository-root", stderr
                )
                with self.assertRaises(CycleCoverageAnalysisError) as context:
                    CycleCoverageAnalyzer(self.configuration, factory).analyze(
                        self.cycle_id, self.build_b, {self.build_b: self.input_b}
                    )
                self.assertEqual(context.exception.code, expected)

    def test_staging_failure_never_publishes_complete_result(self) -> None:
        """Moves partial evidence to .failed without creating a complete success tree."""

        source = "// base\nlet x = 1\n"
        sha_b = self._commit_source("Sources/File.swift", source, "build B")
        self._register_build(self.build_b, sha_b, 2)
        self._single_result(self.build_b, self.input_b, sha_b, self._coverage(source, []))

        with self.assertRaises(CycleCoverageAnalysisError) as context:
            FailingPublishAnalyzer(self.configuration).analyze(
                self.cycle_id, self.build_b, {self.build_b: self.input_b}
            )

        self.assertEqual(context.exception.code, "cycle_analysis_internal_error")
        root = self.analysis_root.parent / "cycle-coverage"
        self.assertFalse(any(root.glob(self.cycle_id + "/" + self.build_b + "/*/complete.json")))
        self.assertTrue(any((root / ".failed" / self.cycle_id / self.build_b).glob("*/failure.json")))

    def test_existing_cumulative_result_tampering_is_rejected(self) -> None:
        """Never reuses completion-protected cumulative output after byte changes."""

        source = "// base\nlet x = 1\n"
        sha_b = self._commit_source("Sources/File.swift", source, "build B")
        self._register_build(self.build_b, sha_b, 2)
        self._single_result(self.build_b, self.input_b, sha_b, self._coverage(source, []))
        first = self._analyze(self.build_b, {self.build_b: self.input_b})
        (first.output_directory / "summary.json").write_text("{}", encoding="utf-8")

        with self.assertRaises(CycleCoverageAnalysisError) as context:
            self._analyze(self.build_b, {self.build_b: self.input_b})

        self.assertEqual(context.exception.code, "existing_cycle_result_invalid")

    def test_export_or_selected_build_set_changes_cycle_identity(self) -> None:
        """Includes each selected immutable export and Build input in the result identity."""

        source = "// base\nlet x = 1\n"
        sha_a = self._commit_source("Sources/File.swift", source, "build A")
        sha_b = self._commit("build B", allow_empty=True)
        self._register_build(self.build_a, sha_a, 1)
        self._register_build(self.build_b, sha_b, 2)
        zero = self._coverage(source, [("Sources/File.swift", 2, 1, 2, 10, 0, "x")])
        hit = self._coverage(source, [("Sources/File.swift", 2, 1, 2, 10, 1, "x")])
        self._single_result(self.build_a, self.input_a, sha_a, hit)
        self._single_result(self.build_b, self.input_b, sha_b, zero)
        alternate_input = "d" * 64
        self._single_result(self.build_b, alternate_input, sha_b, hit)

        current_only = self._analyze(self.build_b, {self.build_b: self.input_b})
        with_history = self._analyze(
            self.build_b, {self.build_a: self.input_a, self.build_b: self.input_b}
        )
        alternate = self._analyze(self.build_b, {self.build_b: alternate_input})

        self.assertEqual(len({current_only.cycle_input_set_sha256, with_history.cycle_input_set_sha256, alternate.cycle_input_set_sha256}), 3)
        self.assertEqual(current_only.summary["lines"]["uncovered"], 1)
        self.assertEqual(with_history.summary["lines"]["covered"], 1)
        self.assertEqual(alternate.summary["lines"]["covered"], 1)

    def test_equal_timestamp_build_order_is_stable(self) -> None:
        """Uses stable Build IDs as a deterministic tie-breaker without input-order drift."""

        source = "// base\nlet x = 1\n"
        sha_a = self._commit_source("Sources/File.swift", source, "build A")
        sha_b = self._commit("build B", allow_empty=True)
        self._register_build(self.build_a, sha_a, 2)
        self._register_build(self.build_b, sha_b, 2)
        payload = self._coverage(source, [("Sources/File.swift", 2, 1, 2, 10, 1, "x")])
        self._single_result(self.build_a, self.input_a, sha_a, payload)
        self._single_result(self.build_b, self.input_b, sha_b, payload)

        first = self._analyze(self.build_b, {self.build_b: self.input_b, self.build_a: self.input_a})
        second = self._analyze(self.build_b, {self.build_a: self.input_a, self.build_b: self.input_b})

        self.assertEqual(first.cycle_input_set_sha256, second.cycle_input_set_sha256)
        self.assertTrue(second.idempotent_replay)

    def _analyze(
        self, current_build_id: str, inputs: Mapping[str, str], cycle_id: Optional[str] = None
    ):
        """Runs the production analyzer against isolated fixtures."""

        return CycleCoverageAnalyzer(self.configuration).analyze(
            cycle_id or self.cycle_id, current_build_id, inputs
        )

    def _create_database(self) -> None:
        """Creates only the persisted columns read by the production validators."""

        self.database.parent.mkdir(parents=True)
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
                    debug_dylib_uuids_json TEXT, dsym_uuids_json TEXT,
                    created_at TEXT
                );
                """
            )
            connection.executemany(
                "INSERT INTO coverage_cycles VALUES (?)",
                [(self.cycle_id,), (self.other_cycle_id,)],
            )

    def _register_build(
        self, build_id: str, git_sha: str, order: int, cycle_id: Optional[str] = None
    ) -> None:
        """Registers one ready Build with exact immutable artifact bytes."""

        owner = cycle_id or self.cycle_id
        main = ("main-" + build_id).encode("utf-8")
        dsym = ("dsym-" + build_id).encode("utf-8")
        main_sha = hashlib.sha256(main).hexdigest()
        dsym_sha = hashlib.sha256(dsym).hexdigest()
        macho_uuid = str(uuid.uuid5(uuid.NAMESPACE_URL, build_id))
        manifest = {
            "schema_version": 1,
            "build_id": build_id,
            "cycle_id": owner,
            "git_sha": git_sha,
            "target_sha_at_build": self.base_sha,
            "merge_base_sha": self.base_sha,
            "coverage_binary": "main_executable",
            "macho_uuids": {"main_executable": [macho_uuid], "dsym": [macho_uuid]},
            "artifacts": {
                "main_executable": {"filename": "ExampleApp", "size": len(main), "sha256": main_sha},
                "dsym": {"filename": "ExampleApp.app.dSYM.zip", "archive_format": "zip", "size": len(dsym), "sha256": dsym_sha},
            },
        }
        canonical = self._canonical(manifest)
        directory = self.data_dir / "cycles" / owner / "builds" / build_id
        artifacts = directory / "artifacts"
        artifacts.mkdir(parents=True)
        (directory / "manifest.json").write_bytes(canonical + b"\n")
        (artifacts / "main_executable.bin").write_bytes(main)
        (artifacts / "dsym.zip").write_bytes(dsym)
        with sqlite3.connect(str(self.database)) as connection:
            connection.execute(
                "INSERT INTO builds VALUES (?, ?, ?, ?, ?, 'main_executable', 'ready', ?, ?, ?, ?, NULL, NULL, ?, ?, ?, NULL, ?, ?)",
                (
                    build_id,
                    owner,
                    git_sha,
                    self.base_sha,
                    self.base_sha,
                    hashlib.sha256(canonical).hexdigest(),
                    canonical.decode("utf-8"),
                    main_sha,
                    len(main),
                    dsym_sha,
                    len(dsym),
                    json.dumps([macho_uuid]),
                    json.dumps([macho_uuid]),
                    "2026-09-01T00:00:{:02d}+00:00".format(order),
                ),
            )

    def _single_result(
        self, build_id: str, input_sha: str, git_sha: str, export: Dict[str, object]
    ) -> None:
        """Publishes one minimal completion-protected single-Build LLVM fixture."""

        with sqlite3.connect(str(self.database)) as connection:
            connection.row_factory = sqlite3.Row
            build = dict(connection.execute("SELECT * FROM builds WHERE id = ?", (build_id,)).fetchone())
        directory = self._single_directory(build_id, input_sha)
        directory.mkdir(parents=True)
        build_identity = {
            "git_sha": git_sha,
            "target_sha_at_build": self.base_sha,
            "merge_base_sha": self.base_sha,
            "manifest_sha256": build["manifest_sha256"],
        }
        inputs = {
            "schema_version": 1,
            "cycle_id": build["cycle_id"],
            "build_id": build_id,
            "input_set_sha256": input_sha,
            "build": build_identity,
            "runs": [],
        }
        summary = {
            "schema_version": 1,
            "status": "complete",
            "cycle_id": build["cycle_id"],
            "build_id": build_id,
            "input_set_sha256": input_sha,
            **build_identity,
            "run_ids": [],
        }
        for name, payload in (("inputs.json", inputs), ("summary.json", summary), ("llvm-cov-export.json", export)):
            (directory / name).write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
        completion = {
            "schema_version": 1,
            "status": "complete",
            "input_set_sha256": input_sha,
            "outputs": self._output_identities(directory),
        }
        (directory / "completion.json").write_text(json.dumps(completion, sort_keys=True), encoding="utf-8")

    def _coverage(self, source: str, regions: Sequence[Region]) -> Dict[str, object]:
        """Builds a supported minimal llvm-cov export with exact absolute paths."""

        paths = sorted({region[0] for region in regions})
        files = [
            {"filename": str(self.repository / path), "segments": [], "summary": {}}
            for path in paths
        ]
        functions: List[Dict[str, object]] = []
        for path, start_line, start_column, end_line, end_column, count, name in regions:
            functions.append(
                {
                    "name": name,
                    "filenames": [str(self.repository / path)],
                    "regions": [[start_line, start_column, end_line, end_column, count, 0, 0, 0]],
                }
            )
        return {
            "type": "llvm.coverage.json.export",
            "version": "2.0.1",
            "data": [{"files": files, "functions": functions, "totals": {}}],
        }

    def _commit_source(self, path: str, source: str, message: str) -> str:
        """Writes one source revision and returns its exact commit object."""

        self._write_files({path: source})
        return self._commit(message)

    def _write_files(self, files: Mapping[str, str]) -> None:
        """Writes controlled fixture files below the temporary repository."""

        for relative, content in files.items():
            path = self.repository / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")

    def _commit(self, message: str, allow_empty: bool = False) -> str:
        """Commits all fixture changes without invoking a shell."""

        self._git("-C", str(self.repository), "add", "-A")
        arguments = ["-C", str(self.repository), "commit", "-q", "-m", message]
        if allow_empty:
            arguments.append("--allow-empty")
        self._git(*arguments)
        return self._git("-C", str(self.repository), "rev-parse", "HEAD").stdout.strip()

    def _git(self, *arguments: str) -> subprocess.CompletedProcess:
        """Runs one fixed test-only Git argument array and requires success."""

        return subprocess.run(
            ["git", *arguments],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )

    def _single_directory(self, build_id: str, input_sha: str) -> Path:
        """Returns one immutable single-Build fixture directory."""

        return self.analysis_root / build_id / input_sha

    def _lines(self, directory: Path) -> List[Dict[str, object]]:
        """Loads cumulative line evidence from one completed result."""

        return json.loads((directory / "cumulative-line-coverage.json").read_text())["lines"]

    def _output_identities(self, directory: Path) -> Dict[str, Dict[str, object]]:
        """Hashes every prior-result file except its completion marker."""

        result = {}
        for path in sorted(item for item in directory.iterdir() if item.is_file()):
            if path.name == "completion.json":
                continue
            data = path.read_bytes()
            result[path.name] = {"size": len(data), "sha256": hashlib.sha256(data).hexdigest()}
        return result

    def _tree_identity(self, roots: Sequence[Path]) -> Dict[str, str]:
        """Hashes protected logical inputs for read-only assertions."""

        result = {}
        for root in roots:
            paths = [root] if root.is_file() else sorted(item for item in root.rglob("*") if item.is_file())
            for path in paths:
                key = str(path.relative_to(self.root))
                result[key] = hashlib.sha256(path.read_bytes()).hexdigest()
        return result

    def _canonical(self, payload: object) -> bytes:
        """Matches production semantic JSON hashing."""

        return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")


if __name__ == "__main__":
    unittest.main()
