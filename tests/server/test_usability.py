#!/usr/bin/env python3
"""HTTP tests for MR Cycle creation and the Cycle browser shell."""

import http.client
import json
import logging
import sqlite3
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path
from ioscoverage.runtime import RuntimePaths
from tests.helpers import project_registry

from ioscoverage.integrations.gitlab_mr import GitLabMRClientError, GitLabMergeRequest
from server import create_server


class FakeGitLabMRClient:
    """Provides deterministic MR data without reading a credential or using a network."""

    def __init__(self) -> None:
        """Starts with one valid open MR and no injected failure."""

        self.calls = []
        self.error = None
        self.merge_request = GitLabMergeRequest(
            project="ExampleApp",
            project_display_name="Example App",
            iid=42,
            web_url="https://git.example.com/mobile/example-app/merge_requests/42",
            state="opened",
            source_branch="feat/code_coverage",
            target_branch="master",
            base_sha="a" * 40,
            start_sha="b" * 40,
            head_sha="c" * 40,
        )

    def fetch(self, mr_url):
        """Returns the configured response or raises one configured safe error."""

        self.calls.append(mr_url)
        if self.error is not None:
            raise self.error
        return self.merge_request


class UsabilityServerTests(unittest.TestCase):
    """Verifies the preferred Cycle entrypoint without touching production state."""

    def setUp(self) -> None:
        """Starts one isolated service with an injected GitLab boundary."""

        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)
        self.repository = self.root / "ExampleApp"
        subprocess.run(
            ["git", "init", str(self.repository)],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        self.database = self.root / "instance" / "coverage.sqlite3"
        self.operator_token = "operator-component-token"
        self.run_token = "restricted-component-token"
        self.gitlab = FakeGitLabMRClient()
        logger = logging.getLogger("usability-server-" + str(id(self)))
        logger.handlers = [logging.NullHandler()]
        logger.propagate = False
        self.server = create_server(
            "127.0.0.1",
            0,
            self.operator_token,
            RuntimePaths.from_root(self.root),
            project_registry(self.repository),
            logger=logger,
            start_analysis_scheduler=False,
            run_upload_token=self.run_token,
            gitlab_mr_client=self.gitlab,
        )
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self) -> None:
        """Stops the isolated server and removes all temporary state."""

        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.temporary_directory.cleanup()

    def request(self, method, path, payload=None, token=None):
        """Sends one HTTP request and returns status, headers, and raw bytes."""

        body = b""
        headers = {}
        if payload is not None:
            body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
            headers.update({"Content-Type": "application/json", "Content-Length": str(len(body))})
        if token is not None:
            headers["Authorization"] = "Bearer " + token
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=3)
        connection.request(method, path, body=body, headers=headers)
        response = connection.getresponse()
        result = (response.status, dict(response.getheaders()), response.read())
        connection.close()
        return result

    def create_from_mr(self):
        """Creates the configured MR Cycle through the preferred two-field request."""

        return self.request(
            "POST",
            "/api/v1/cycles",
            {
                "schema_version": 1,
                "mr_url": "https://git.example.com/mobile/example-app/merge_requests/42/diffs",
            },
            self.operator_token,
        )

    def test_mr_response_maps_and_second_request_reuses_cycle(self) -> None:
        """Creates once, maps the fixed MR snapshot, and returns the same immutable ID later."""

        first_status, _, first_body = self.create_from_mr()
        second_status, _, second_body = self.create_from_mr()
        first = json.loads(first_body)
        second = json.loads(second_body)
        self.assertEqual(first_status, 201)
        self.assertEqual(first["status"], "created")
        self.assertEqual(second_status, 200)
        self.assertEqual(second["status"], "reused")
        self.assertEqual(first["cycle"]["id"], second["cycle"]["id"])
        self.assertEqual(first["cycle"]["project"], "ExampleApp")
        self.assertEqual(first["cycle"]["mr_iid"], "42")
        self.assertEqual(first["cycle"]["target_branch"], "master")
        self.assertEqual(first["cycle"]["base_sha"], "a" * 40)
        self.assertEqual(first["cycle"]["head_sha"], "c" * 40)
        with sqlite3.connect(str(self.database)) as connection:
            count = connection.execute(
                "SELECT COUNT(*) FROM coverage_cycles WHERE project = 'ExampleApp' AND mr_iid = '42'"
            ).fetchone()[0]
        self.assertEqual(count, 1)

    def test_gitlab_failure_creates_no_partial_cycle(self) -> None:
        """Propagates a safe upstream error before any SQLite insert occurs."""

        self.gitlab.error = GitLabMRClientError(
            "gitlab_auth_failed", "GitLab credential was rejected", 502
        )
        status, _, body = self.create_from_mr()
        self.assertEqual(status, 502)
        self.assertEqual(json.loads(body)["error"], "gitlab_auth_failed")
        with sqlite3.connect(str(self.database)) as connection:
            count = connection.execute("SELECT COUNT(*) FROM coverage_cycles").fetchone()[0]
        self.assertEqual(count, 0)
        self.assertNotIn(self.operator_token, body.decode("utf-8"))
        self.assertNotIn(self.run_token, body.decode("utf-8"))

    def test_legacy_full_cycle_json_remains_compatible(self) -> None:
        """Keeps existing scripts able to provide the complete immutable Cycle snapshot."""

        status, _, body = self.request(
            "POST",
            "/api/v1/cycles",
            {
                "schema_version": 1,
                "project": "ExampleApp",
                "mr_iid": "legacy",
                "mr_url": "https://legacy.invalid/mr/1",
                "target_branch": "master",
                "base_sha": "d" * 40,
                "head_sha": "e" * 40,
            },
            self.operator_token,
        )
        self.assertEqual(status, 201)
        self.assertEqual(json.loads(body)["status"], "created")
        self.assertEqual(self.gitlab.calls, [])

    def test_cycle_list_is_updated_first_with_aggregated_counts(self) -> None:
        """Returns recent Cycles first and computes Build/Run counts without denormalization."""

        _, _, created_body = self.create_from_mr()
        cycle_id = json.loads(created_body)["cycle"]["id"]
        with sqlite3.connect(str(self.database)) as connection:
            connection.execute(
                """INSERT INTO builds (
                    id,cycle_id,schema_version,git_sha,target_sha_at_build,merge_base_sha,
                    coverage_binary,bundle_id,app_version,bundle_version,xcode_version,
                    swift_version,main_executable_uuids_json,debug_dylib_uuids_json,
                    dsym_uuids_json,manifest_sha256,manifest_json,main_executable_sha256,
                    main_executable_size,debug_dylib_sha256,debug_dylib_size,dsym_sha256,
                    dsym_size,artifact_state,pending_runs_promoted,created_at
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    "build-1", cycle_id, 1, "1" * 40, "2" * 40, "3" * 40,
                    "main_executable", "com.test", "1", "1", "26.3", "6.2",
                    "[]", None, "[]", "4" * 64, "{}", "5" * 64, 1,
                    None, None, "6" * 64, 1, "ready", 0, "2026-01-01T00:00:00+00:00",
                ),
            )
            connection.execute(
                """INSERT INTO runs (
                    id,cycle_id,build_id,schema_version,device_id,process_id,launch_id,
                    captured_at,received_at,metadata_sha256,metadata_json,payload_sha256,
                    payload_size,state,analyzable,error_message
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    "run-1", cycle_id, "build-1", 1, "device", "process", "launch",
                    "2026-01-01T00:00:00+00:00", "2026-01-01T00:00:00+00:00",
                    "7" * 64, "{}", "8" * 64, 1, "uploaded", 1, None,
                ),
            )
            connection.execute(
                "UPDATE coverage_cycles SET updated_at = '2030-01-01T00:00:00+00:00' WHERE id = ?",
                (cycle_id,),
            )
        status, _, body = self.request("GET", "/api/v1/cycles", token=self.operator_token)
        cycles = json.loads(body)["cycles"]
        self.assertEqual(status, 200)
        self.assertEqual(cycles[0]["id"], cycle_id)
        self.assertEqual(cycles[0]["build_count"], 1)
        self.assertEqual(cycles[0]["run_count"], 1)

    def test_cycles_redirect_and_data_free_shell(self) -> None:
        """Serves a static shell with the MR form and no persisted Cycle or credentials."""

        _, _, created_body = self.create_from_mr()
        cycle_id = json.loads(created_body)["cycle"]["id"]
        status, headers, _ = self.request("GET", "/cycles")
        self.assertEqual(status, 308)
        self.assertEqual(headers["Location"], "/cycles/")
        status, _, body = self.request("GET", "/cycles/")
        text = body.decode("utf-8")
        self.assertEqual(status, 200)
        self.assertIn("id=\"create-form\"", text)
        self.assertIn("/api/v1/cycles", text)
        self.assertIn("ios-coverage-operator-token", text)
        self.assertNotIn(cycle_id, text)
        self.assertNotIn(self.operator_token, text)
        self.assertNotIn(self.run_token, text)

    def test_restricted_token_cannot_create_or_list_cycles(self) -> None:
        """Rejects the iOS credential before it can read or mutate Cycle resources."""

        list_status, _, list_body = self.request(
            "GET", "/api/v1/cycles", token=self.run_token
        )
        create_status, _, create_body = self.request(
            "POST",
            "/api/v1/cycles",
            {"schema_version": 1, "mr_url": "https://git.example.com/mobile/example-app/merge_requests/42"},
            self.run_token,
        )
        self.assertEqual(list_status, 403)
        self.assertEqual(create_status, 403)
        self.assertEqual(json.loads(list_body)["error"], "insufficient_scope")
        self.assertEqual(json.loads(create_body)["error"], "insufficient_scope")
        self.assertEqual(self.gitlab.calls, [])


if __name__ == "__main__":
    unittest.main()
