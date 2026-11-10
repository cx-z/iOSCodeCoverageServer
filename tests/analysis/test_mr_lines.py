"""Differential safety checks for memory-bounded identity proofs and ambiguous ties."""

import difflib
import random
import unittest
from unittest.mock import patch
from ioscoverage.analysis.mapping.mr_lines import verified_line_mapping, _banded_pairs, _subsequence_pairs


class BoundedLineProofTests(unittest.TestCase):
    """Small full-alignment results act as an oracle for optimized proof paths."""

    def test_bounded_proofs_agree_with_full_alignment_under_repeated_text(self):
        """Checks forced positives and ambiguous negatives over varied deterministic ties."""
        rng = random.Random(321)
        for case in range(1500):
            old = [rng.choice('abc')+'\n' for _ in range(rng.randrange(1,10))]
            new = [rng.choice('abc')+'\n' for _ in range(rng.randrange(1,10))]
            pairs = {j+k+1:i+k+1 for i,j,count in difflib.SequenceMatcher(None,old,new,autojunk=False).get_matching_blocks()
                     for k in range(count)}
            expected = verified_line_mapping(''.join(old),''.join(new),pairs)
            with self.subTest(case=case):
                self.assertEqual(_banded_pairs(old,new,pairs,len(old)+len(new)-2*len(pairs)),expected)
                embedded = _subsequence_pairs(old,new,pairs)
                if embedded is not None:
                    self.assertEqual(embedded,expected)

    def test_exhausted_budgets_never_authorize_optional_pairs(self):
        """Exercises the final fallback with reordered unique and repeated rows."""
        rng = random.Random(714)
        for case in range(300):
            old = [str(rng.randrange(8))+'\n' for _ in range(12)]
            new = old[:]
            rng.shuffle(new)
            pairs = {j+k+1:i+k+1 for i,j,count in difflib.SequenceMatcher(None,old,new,autojunk=False).get_matching_blocks()
                     for k in range(count)}
            expected, uncertain = verified_line_mapping(''.join(old), ''.join(new), pairs)
            with patch('ioscoverage.analysis.mapping.mr_lines.MAX_ALIGNMENT_CELLS', 0):
                actual, ambiguous = verified_line_mapping(''.join(old), ''.join(new), pairs)
            with self.subTest(case=case):
                self.assertTrue(actual.items() <= expected.items())
                self.assertTrue(uncertain <= ambiguous)
