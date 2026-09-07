#!/usr/bin/env python3
"""Component tests for the dependency-free iOS network probe."""

import hashlib
import http.client
import io
import json
import logging
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path
from ioscoverage.runtime import RuntimePaths
from tests.helpers import project_registry
from typing import Dict, Tuple

from server import create_server


class ProbeServerTests(unittest.TestCase):
    """Exercises the HTTP boundary and persisted integrity metadata."""

    def setUp(self) -> None:
        """Starts an isolated loopback server with a small test upload limit."""

        self.temporary_directory = tempfile.TemporaryDirectory()
        self.test_root = Path(self.temporary_directory.name)
        self.data_dir = self.test_root / "data"
        self.repository = self.test_root / "ExampleApp"
        subprocess.run(
            ["git", "init", str(self.repository)],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        self.token = "component-test-token"
        self.run_upload_token = "component-run-upload-token"
        self.log_stream = io.StringIO()
        self.logger = logging.getLogger("ios_coverage_probe_test_{}".format(id(self)))
        self.logger.handlers.clear()
        self.logger.propagate = False
        self.logger.setLevel(logging.INFO)
        self.logger.addHandler(logging.StreamHandler(self.log_stream))
        self.server = create_server(
            "127.0.0.1",
            0,
            self.token,
            RuntimePaths.from_root(self.test_root),
            project_registry(self.repository),
            1024,
            self.logger,
            start_analysis_scheduler=False,
            run_upload_token=self.run_upload_token,
        )
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self) -> None:
        """Stops the server and removes all isolated test data."""

        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.temporary_directory.cleanup()

    def request(
        self, method: str, path: str, body: bytes = b"", headers: Dict[str, str] = None
    ) -> Tuple[int, Dict[str, object]]:
        """Sends one request and parses its JSON response."""

        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=2)
        connection.request(method, path, body=body, headers=headers or {})
        response = connection.getresponse()
        payload = json.loads(response.read().decode("utf-8"))
        connection.close()
        return response.status, payload

    def multipart(self, payload: bytes, declared_sha256: str) -> Tuple[bytes, str]:
        """Builds the small multipart body used by upload boundary tests."""

        boundary = "ios-coverage-test-probe-boundary"
        body = (
            "--{0}\r\n"
            'Content-Disposition: form-data; name="sha256"\r\n\r\n'
            "{1}\r\n"
            "--{0}\r\n"
            'Content-Disposition: form-data; name="file"; filename="probe.bin"\r\n'
            "Content-Type: application/octet-stream\r\n\r\n"
        ).format(boundary, declared_sha256).encode("ascii")
        body += payload + ("\r\n--{}--\r\n".format(boundary)).encode("ascii")
        return body, "multipart/form-data; boundary={}".format(boundary)

    def upload(self, payload: bytes, declared_sha256: str) -> Tuple[int, Dict[str, object]]:
        """Uploads a probe with valid authentication."""

        body, content_type = self.multipart(payload, declared_sha256)
        return self.request(
            "POST",
            "/api/v1/probes",
            body,
            {
                "Authorization": "Bearer " + self.token,
                "Content-Type": content_type,
                "Content-Length": str(len(body)),
            },
        )

    def upload_with_run_token(
        self, payload: bytes, declared_sha256: str
    ) -> Tuple[int, Dict[str, object]]:
        """Uploads one probe with the restricted iOS credential."""

        body, content_type = self.multipart(payload, declared_sha256)
        return self.request(
            "POST",
            "/api/v1/probes",
            body,
            {
                "Authorization": "Bearer " + self.run_upload_token,
                "Content-Type": content_type,
                "Content-Length": str(len(body)),
            },
        )

    def test_health_does_not_require_token(self) -> None:
        """Confirms the public health response exposes only probe-safe fields."""

        status, payload = self.request("GET", "/health")
        self.assertEqual(status, 200)
        self.assertEqual(payload["status"], "ok")
        self.assertIn("instance_id", payload)
        self.assertNotIn("token", payload)
        self.assertNotIn("data_dir", payload)

    def test_upload_requires_bearer_token(self) -> None:
        """Confirms unauthenticated devices cannot persist probe data."""

        body, content_type = self.multipart(b"hello", hashlib.sha256(b"hello").hexdigest())
        status, payload = self.request(
            "POST",
            "/api/v1/probes",
            body,
            {"Content-Type": content_type, "Content-Length": str(len(body))},
        )
        self.assertEqual(status, 401)
        self.assertEqual(payload["error"], "invalid_token")
        self.assertEqual(list(self.data_dir.glob("*.json")), [])
        self.assertEqual(list(self.data_dir.glob("*.bin")), [])

    def test_run_upload_token_can_probe_but_cannot_list_cycles(self) -> None:
        """Scopes the iOS credential to probe/upload writes and denies management reads."""

        source = b"restricted probe"
        status, _ = self.upload_with_run_token(source, hashlib.sha256(source).hexdigest())
        self.assertEqual(status, 201)
        status, payload = self.request(
            "GET",
            "/api/v1/cycles",
            headers={"Authorization": "Bearer " + self.run_upload_token},
        )
        self.assertEqual(status, 403)
        self.assertEqual(payload["error"], "insufficient_scope")

    def test_unknown_token_is_unauthorized_not_scope_denied(self) -> None:
        """Distinguishes an invalid credential from a valid restricted credential."""

        status, payload = self.request(
            "GET",
            "/api/v1/cycles",
            headers={"Authorization": "Bearer definitely-invalid-token"},
        )
        self.assertEqual(status, 401)
        self.assertEqual(payload["error"], "invalid_token")

    def test_valid_upload_persists_matching_payload_and_metadata(self) -> None:
        """Confirms size and SHA-256 match the bytes persisted by the server."""

        source = b"iOS Coverage Safari upload\n"
        expected_sha256 = hashlib.sha256(source).hexdigest()
        status, payload = self.upload(source, expected_sha256)
        self.assertEqual(status, 201)
        self.assertTrue(payload["sha256_matches"])
        self.assertEqual(payload["size"], len(source))
        self.assertEqual(payload["sha256"], expected_sha256)

        probe_id = payload["probe_id"]
        saved_payload = (self.data_dir / (probe_id + ".bin")).read_bytes()
        saved_metadata = json.loads(
            (self.data_dir / (probe_id + ".json")).read_text(encoding="utf-8")
        )
        self.assertEqual(saved_payload, source)
        self.assertEqual(saved_metadata["size"], len(source))
        self.assertEqual(saved_metadata["sha256"], expected_sha256)

    def test_sha256_mismatch_is_rejected_without_files(self) -> None:
        """Confirms a mismatched hash is diagnosed and never persisted."""

        status, payload = self.upload(b"actual", hashlib.sha256(b"different").hexdigest())
        self.assertEqual(status, 422)
        self.assertEqual(payload["error"], "sha256_mismatch")
        self.assertEqual(list(self.data_dir.glob("*.json")), [])
        self.assertEqual(list(self.data_dir.glob("*.bin")), [])

    def test_file_over_limit_is_rejected_without_files(self) -> None:
        """Confirms the configured file-size boundary is enforced after parsing."""

        source = b"x" * 1025
        status, payload = self.upload(source, hashlib.sha256(source).hexdigest())
        self.assertEqual(status, 413)
        self.assertIn(payload["error"], ("request_too_large", "file_too_large"))
        self.assertEqual(list(self.data_dir.glob("*.json")), [])
        self.assertEqual(list(self.data_dir.glob("*.bin")), [])

    def test_structured_log_covers_authentication_and_success_without_token(self) -> None:
        """Confirms persistent diagnostics explain failures without leaking credentials."""

        body, content_type = self.multipart(b"hello", hashlib.sha256(b"hello").hexdigest())
        unauthorized_status, _ = self.request(
            "POST",
            "/api/v1/probes",
            body,
            {"Content-Type": content_type, "Content-Length": str(len(body))},
        )
        accepted_status, _ = self.upload(b"hello", hashlib.sha256(b"hello").hexdigest())
        self.assertEqual(unauthorized_status, 401)
        self.assertEqual(accepted_status, 201)

        raw_log = self.log_stream.getvalue()
        records = [json.loads(line) for line in raw_log.splitlines()]
        events = {record["event"] for record in records}
        self.assertIn("authentication_failed", events)
        self.assertIn("upload_body_received", events)
        self.assertIn("upload_accepted", events)
        self.assertIn("request_completed", events)
        self.assertNotIn(self.token, raw_log)
        completed_statuses = {
            record["status"] for record in records if record["event"] == "request_completed"
        }
        self.assertEqual(completed_statuses, {401, 201})


if __name__ == "__main__":
    unittest.main()
