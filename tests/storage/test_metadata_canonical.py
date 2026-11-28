"""Cross-language canonical metadata golden shared with CoverageAggregationTests."""
import hashlib
import unittest
from ioscoverage.storage.validation import ValidationMixin


class MetadataCanonicalTests(unittest.TestCase):
    def test_swift_python_golden(self):
        """Retains Unicode, escaping, integer and array identity exactly as the Swift codec."""
        value = {"z": ["中/😀\u2028\n\"\\\x01", 9223372036854775807], "a": {"process_id": 42}}
        encoded = ValidationMixin()._canonical_json_bytes(value)
        self.assertEqual(hashlib.sha256(encoded).hexdigest(), "b32b2a1a11b2dd6b5e3997ccdd2e1aea933804e3cc089b0469337d00c5ead9a3")
