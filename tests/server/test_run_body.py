"""Run decoder framing and allocation-boundary tests without a listening server."""

from email.message import Message
import gzip
import io
import unittest
from unittest.mock import patch

from ioscoverage.server.http.run_body import decoded_run_body
from ioscoverage.storage.coverage_store import CoverageStoreError


class RunBodyTests(unittest.TestCase):
    """Checks exact framing, bounded decoding, and temporary stream ownership."""

    def headers(self, length, encoding="gzip"):
        """Creates one declared-length request header block."""
        headers = Message()
        headers["Content-Length"] = str(length)
        headers["Content-Encoding"] = encoding
        return headers

    def test_exact_limit_and_cleanup(self):
        """A valid single member round-trips at the bound and closes after use."""
        raw = bytes(range(256)) * 1000
        encoded = gzip.compress(raw)
        with decoded_run_body(self.headers(len(encoded)), io.BytesIO(encoded), len(raw)) as (stream, size):
            self.assertEqual(size, len(raw))
            self.assertEqual(stream.read(), raw)
        self.assertTrue(stream.closed)

    def test_truncated_wire_and_invalid_framing(self):
        """Early EOF, repeated lengths, transfer coding and encoding chains fail closed."""
        encoded = gzip.compress(b"payload")
        variants = [self.headers(len(encoded) + 1), self.headers(len(encoded)), self.headers(len(encoded)), self.headers(len(encoded), "gzip, identity")]
        variants[1]["Content-Length"] = str(len(encoded))
        variants[2]["Transfer-Encoding"] = "chunked"
        for headers in variants:
            with self.subTest(headers=list(headers.items())), self.assertRaises(CoverageStoreError):
                with decoded_run_body(headers, io.BytesIO(encoded), 100):
                    self.fail("invalid body must never be published")

    def test_failure_closes_spool_and_rejects_crc(self):
        """Damaged gzip trailers cannot yield bytes and leak no temporary stream."""
        encoded = bytearray(gzip.compress(b"profile"))
        encoded[-8] ^= 1
        spool = io.BytesIO()
        with patch("ioscoverage.server.http.run_body.tempfile.TemporaryFile", return_value=spool):
            with self.assertRaises(CoverageStoreError) as caught:
                with decoded_run_body(self.headers(len(encoded)), io.BytesIO(encoded), 100):
                    self.fail("CRC damage must fail")
        self.assertEqual(caught.exception.code, "invalid_compressed_body")
        self.assertTrue(spool.closed)
