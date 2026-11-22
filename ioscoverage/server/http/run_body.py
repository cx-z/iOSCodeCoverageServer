"""Bounded request decoding owned exclusively by the Run HTTP boundary."""

from contextlib import contextmanager
import tempfile
import zlib

from ioscoverage.storage.coverage_store import CoverageStoreError


@contextmanager
def decoded_run_body(headers, stream, maximum_bytes):
    """Spools one exact-length identity/gzip body and bounds each decoded allocation."""

    if headers.get_all("Transfer-Encoding"):
        raise CoverageStoreError("invalid_request", "transfer coding is not supported")
    lengths = headers.get_all("Content-Length", [])
    if len(lengths) != 1 or not lengths[0].isascii() or not lengths[0].isdigit():
        raise CoverageStoreError("content_length_required", "one numeric Content-Length is required")
    length = int(lengths[0])
    if length <= 0 or length > maximum_bytes:
        raise CoverageStoreError("request_too_large", "encoded Run request exceeds its limit")
    encodings = headers.get_all("Content-Encoding", [])
    encoding = encodings[0].strip().lower() if len(encodings) == 1 else "identity"
    if len(encodings) > 1 or encoding not in ("identity", "gzip"):
        raise CoverageStoreError("unsupported_content_encoding", "Run encoding must be identity or gzip")
    decoder = zlib.decompressobj(16 + zlib.MAX_WBITS) if encoding == "gzip" else None
    with tempfile.TemporaryFile(mode="w+b") as body:
        remaining = length
        size = 0
        while remaining:
            chunk = stream.read(min(65536, remaining))
            if not chunk:
                raise CoverageStoreError("incomplete_request", "Run request body ended early")
            remaining -= len(chunk)
            try:
                while chunk:
                    if decoder is not None:
                        if decoder.eof:
                            raise CoverageStoreError("invalid_compressed_body", "gzip has trailing data")
                        output = decoder.decompress(chunk, min(65536, maximum_bytes - size + 1))
                        chunk = decoder.unconsumed_tail
                        if decoder.unused_data:
                            raise CoverageStoreError("invalid_compressed_body", "gzip has trailing data")
                    else:
                        output, chunk = chunk, b""
                    size += len(output)
                    if size > maximum_bytes:
                        raise CoverageStoreError("request_too_large", "decoded Run request exceeds its limit")
                    body.write(output)
            except zlib.error:
                raise CoverageStoreError("invalid_compressed_body", "invalid gzip Run body") from None
        if decoder is not None and not decoder.eof:
            raise CoverageStoreError("invalid_compressed_body", "gzip Run body is truncated")
        body.seek(0)
        yield body, size
