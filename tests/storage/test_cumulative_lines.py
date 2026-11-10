"""Lossless region-reference encoding and strict malformed-reference rejection."""

import copy
import unittest

from ioscoverage.storage.cumulative_lines import (
    decode_cumulative_lines,
    encode_cumulative_lines,
)


class CumulativeLineCodecTests(unittest.TestCase):
    """Keeps stored decisions intact across inline and referenced representations."""

    def setUp(self):
        """Creates shared multiline evidence with historical audit details."""

        self.decision = {
            "repo_relative_path": "Sources/Feature.swift",
            "current_region": {"start_line": 1, "end_line": 3, "count": 0},
            "covered": True,
            "source": {"kind": "inherited_build", "build_id": "historical"},
            "inheritance_attempts": [{"inherited": True, "reason": "exact_source"}],
        }
        self.table = [self.decision]
        self.lines = [{
            "repo_relative_path": "Sources/Feature.swift",
            "new_line_number": number,
            "source_text": "// 保留源码与证据",
            "state": "covered",
            "regions": [copy.deepcopy(self.decision)],
        } for number in (1, 2, 3)]

    def test_round_trip_preserves_complete_lines_and_shares_region_objects(self):
        """Keeps every field while resolving repeated indices to one local object."""

        before = copy.deepcopy(self.lines)
        encoded = encode_cumulative_lines(self.lines, self.table)
        decoded = decode_cumulative_lines(
            {"schema_version": 2, "lines": encoded}, {"region_mappings": self.table}
        )
        self.assertEqual(decoded, before)
        self.assertEqual(self.lines, before)
        self.assertEqual([line["region_ids"] for line in encoded], [[0], [0], [0]])
        self.assertTrue(all("regions" not in line for line in encoded))
        self.assertIs(decoded[0]["regions"][0], decoded[1]["regions"][0])
        self.assertIs(decoded[0]["regions"][0], self.decision)

    def test_legacy_inline_lines_and_empty_unmeasurable_lines_are_supported(self):
        """Reads existing inline output and keeps empty region references empty."""

        for version in ({}, {"schema_version": 1}):
            self.assertEqual(decode_cumulative_lines({**version, "lines": self.lines}), self.lines)
        line = {**self.lines[0], "state": "not_measurable", "regions": []}
        encoded = encode_cumulative_lines([line], [])
        self.assertEqual(decode_cumulative_lines(
            {"schema_version": 2, "lines": encoded}, {"region_mappings": []}
        ), [line])

    def test_invalid_indices_and_ambiguous_inline_data_are_rejected(self):
        """Rejects bool, negative, dangling, non-integer and mixed references."""

        encoded = encode_cumulative_lines(self.lines, self.table)[0]
        invalid = [
            {**encoded, "region_ids": value}
            for value in (None, "0", [-1], [1], [True], [0.0], ["0"])
        ]
        invalid.append({**encoded, "regions": [self.decision]})
        for line in invalid:
            with self.subTest(line=line), self.assertRaises(ValueError):
                decode_cumulative_lines(
                    {"schema_version": 2, "lines": [line]}, {"region_mappings": self.table}
                )

    def test_missing_malformed_and_cross_file_tables_are_rejected(self):
        """Never attaches unproven or different-file evidence to a changed line."""

        encoded = encode_cumulative_lines(self.lines, self.table)
        bad_tables = [None, {}, {"region_mappings": {}}, {"region_mappings": [None]},
                      {"region_mappings": [{**self.decision, "current_region": None}]},
                      {"region_mappings": [{**self.decision, "repo_relative_path": "Other.swift"}]}]
        for table in bad_tables:
            with self.subTest(table=table), self.assertRaises(ValueError):
                decode_cumulative_lines({"schema_version": 2, "lines": encoded}, table)

    def test_unknown_schema_and_malformed_line_arrays_are_rejected(self):
        """Fails closed rather than guessing a future representation."""

        for payload in ({"schema_version": 3, "lines": []},
                        {"schema_version": True, "lines": []},
                        {"lines": [{"region_ids": []}]},
                        {"schema_version": 1, "lines": [{"regions": [], "region_ids": []}]},
                        {"schema_version": 2, "lines": {}},
                        {"schema_version": 2, "lines": [None]}):
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                decode_cumulative_lines(payload, {"region_mappings": []})

    def test_encoder_rejects_missing_duplicate_or_different_file_decisions(self):
        """Requires every emitted index to identify exactly one complete decision."""

        for table in ([], [self.decision, copy.deepcopy(self.decision)],
                      [{**self.decision, "covered": False}]):
            with self.subTest(table=table), self.assertRaises(ValueError):
                encode_cumulative_lines(self.lines, table)
        lines = [{**self.lines[0], "repo_relative_path": "Other.swift"}]
        with self.assertRaises(ValueError):
            encode_cumulative_lines(lines, self.table)
