#!/usr/bin/env python3
"""Read-only report projection, HTTP boundary, and browser-shell tests."""

import hashlib
import http.client
import json
import logging
import sqlite3
import tempfile
import threading
import unittest
import uuid
from pathlib import Path
from types import SimpleNamespace

from ioscoverage.storage.analysis_state import AnalysisStateStore
from ioscoverage.storage.reports import CoverageReportError, CoverageReportStore
from ioscoverage.web.pages.report import render_coverage_report_page
from server import ProbeConfig, ProbeHTTPServer


class ReportFixture:
    """Publishes one minimal completion-protected cumulative report in a temp root."""

    def __init__(self):
        """Creates immutable result directories and one latest-success state row."""

        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.analysis = self.root / "analysis"
        self.database = self.root / "coverage.sqlite3"
        self.cycle_id = str(uuid.uuid4())
        self.build_id = str(uuid.uuid4())
        self.orchestration_sha = "a" * 64
        self.cycle_sha = "b" * 64
        with sqlite3.connect(str(self.database)) as connection:
            connection.execute("CREATE TABLE coverage_cycles (id TEXT PRIMARY KEY)")
            connection.execute("INSERT INTO coverage_cycles VALUES (?)", (self.cycle_id,))
            connection.execute("PRAGMA user_version = 2")
        self.state_store = AnalysisStateStore(self.database)
        self.cycle_directory = (
            self.analysis
            / "cycle-coverage"
            / self.cycle_id
            / self.build_id
            / self.cycle_sha
        )
        self.orchestration_directory = (
            self.analysis / "cycle-latest" / self.cycle_id / self.orchestration_sha
        )
        self.path = "Sources/Feature One.swift"
        self._publish_cycle_result()
        self._publish_orchestration_result()
        self.state_store.request_analysis(self.cycle_id, "manual_retry")
        self.state_store.claim(self.cycle_id)
        self.state_store.complete_success(
            self.cycle_id,
            1,
            {
                "orchestration_input_sha256": self.orchestration_sha,
                "cycle_input_set_sha256": self.cycle_sha,
                "current_build_id": self.build_id,
                "output_directory": str(self.orchestration_directory),
                "summary": {"covered": 2},
            },
        )
        self.store = CoverageReportStore(self.analysis, self.state_store)

    def close(self):
        """Removes all temporary report and database files."""

        self.temporary.cleanup()

    def _publish_cycle_result(self):
        """Writes one valid cumulative summary and changed-line document."""

        self.cycle_directory.mkdir(parents=True)
        lines = [
            self._line(10, "covered", "return true", "current_build"),
            self._line(11, "covered", "return oldHit", "inherited_build"),
            self._line(12, "uncovered", "return false", None),
            self._line(13, "not_measurable", "// comment", None, regions=False),
            self._line(14, "unknown", "broken", None, regions=False),
        ]
        summary = {
            "schema_version": 1,
            "status": "complete",
            "cycle_id": self.cycle_id,
            "current_build_id": self.build_id,
            "cycle_input_set_sha256": self.cycle_sha,
            "changed_line_coverage_percent": 66.6666666667,
            "lines": {
                "changed_new_side": 5,
                "measurable": 3,
                "covered": 2,
                "uncovered": 1,
                "not_measurable": 1,
                "unknown": 1,
                "deleted_excluded": 0,
                "covered_with_inherited_evidence": 1,
            },
            "per_file": [
                {
                    "repo_relative_path": self.path,
                    "git_status": "M",
                    "changed_new_side": 5,
                    "measurable": 3,
                    "covered": 2,
                    "uncovered": 1,
                    "not_measurable": 1,
                    "unknown": 1,
                    "deleted_excluded": 0,
                }
            ],
        }
        self._write_json(self.cycle_directory / "summary.json", summary)
        self._write_json(
            self.cycle_directory / "cumulative-line-coverage.json", {"lines": lines}
        )
        self._refresh_cycle_completion()

    def _publish_orchestration_result(self):
        """Writes one valid orchestration pointer to the cumulative result."""

        self.orchestration_directory.mkdir(parents=True)
        results = {
            "schema_version": 1,
            "single_build_results": [],
            "current_diff_result": {},
            "cycle_coverage_result": {
                "cycle_input_set_sha256": self.cycle_sha,
                "output_directory": str(self.cycle_directory),
                "completion": self._identity(self.cycle_directory / "complete.json"),
                "summary": self._identity(self.cycle_directory / "summary.json"),
            },
        }
        self._write_json(self.orchestration_directory / "manifest.json", {"fixture": True})
        self._write_json(self.orchestration_directory / "results.json", results)
        self._write_json(
            self.orchestration_directory / "summary.json",
            {"status": "complete", "orchestration_input_sha256": self.orchestration_sha},
        )
        self._refresh_orchestration_completion()

    def _line(self, number, state, source_text, source_kind, regions=True):
        """Returns one production-shaped changed-line fixture."""

        decisions = []
        if regions:
            source = None
            if source_kind is not None:
                source = {
                    "kind": source_kind,
                    "build_id": self.build_id if source_kind == "current_build" else str(uuid.uuid4()),
                    "input_set_sha256": "c" * 64,
                    "git_sha": "d" * 40,
                }
            decisions.append(
                {
                    "covered": state == "covered",
                    "reason": "fixture_region",
                    "current_region": {
                        "start_line": number,
                        "start_column": 1,
                        "end_line": number,
                        "end_column": 8,
                        "count": 1 if state == "covered" else 0,
                    },
                    "source": source,
                }
            )
        return {
            "repo_relative_path": self.path,
            "new_line_number": number,
            "source_text": source_text,
            "source_text_sha256": hashlib.sha256(source_text.encode()).hexdigest(),
            "state": state,
            "reason": "fixture_line",
            "regions": decisions,
        }

    def _refresh_cycle_completion(self):
        """Rehashes every cumulative output after a fixture mutation."""

        outputs = self._directory_identity(self.cycle_directory, "complete.json")
        self._write_json(
            self.cycle_directory / "complete.json",
            {
                "schema_version": 1,
                "status": "complete",
                "cycle_input_set_sha256": self.cycle_sha,
                "outputs": outputs,
            },
        )

    def _refresh_orchestration_completion(self):
        """Rehashes every orchestration output after a fixture mutation."""

        outputs = self._directory_identity(self.orchestration_directory, "complete.json")
        self._write_json(
            self.orchestration_directory / "complete.json",
            {
                "schema_version": 1,
                "status": "complete",
                "orchestration_input_sha256": self.orchestration_sha,
                "outputs": outputs,
            },
        )

    def _directory_identity(self, directory, completion_name):
        """Returns completion descriptors for all non-marker files."""

        return {
            path.relative_to(directory).as_posix(): self._identity(path)
            for path in sorted(directory.rglob("*"))
            if path.is_file() and path.name != completion_name
        }

    def _identity(self, path):
        """Returns one exact test-file size and SHA-256."""

        payload = path.read_bytes()
        return {"size": len(payload), "sha256": hashlib.sha256(payload).hexdigest()}

    def _write_json(self, path, payload):
        """Writes deterministic fixture JSON bytes."""

        path.write_text(json.dumps(payload, sort_keys=True) + "\n", encoding="utf-8")


class CoverageReportStoreTests(unittest.TestCase):
    """Verifies report integrity, source projection, and opaque file selection."""

    def setUp(self):
        """Creates one completed temporary report."""

        self.fixture = ReportFixture()

    def tearDown(self):
        """Removes the temporary report."""

        self.fixture.close()

    def test_summary_and_file_counts_are_projected(self):
        """Returns the four requested status counts without internal paths."""

        report = self.fixture.store.get_report(self.fixture.cycle_id)
        self.assertEqual(
            (report["summary"]["covered"], report["summary"]["uncovered"]), (2, 1)
        )
        self.assertEqual(
            (report["summary"]["not_measurable"], report["summary"]["unknown"]),
            (1, 1),
        )
        self.assertEqual(report["files"][0]["uncovered"], 1)
        self.assertNotIn("output_directory", json.dumps(report))

    def test_path_mapping_failure_keeps_previous_report_readable(self):
        """Retains the exact successful report after a safe mapping failure."""

        previous = self.fixture.store.get_report(self.fixture.cycle_id)
        self.fixture.state_store.request_analysis(self.fixture.cycle_id, "manual_retry")
        generation = self.fixture.state_store.claim(self.fixture.cycle_id)
        self.fixture.state_store.complete_failure(
            self.fixture.cycle_id, generation, "current_diff",
            "coverage_path_mapping_failed", "compiler coverage could not be mapped",
        )
        report = self.fixture.store.get_report(self.fixture.cycle_id)
        self.assertEqual(report["generation"], previous["generation"])
        self.assertEqual(report["summary"], previous["summary"])
        self.assertEqual(report["files"], previous["files"])
        state = self.fixture.state_store.get_state(self.fixture.cycle_id)
        self.assertEqual(state["status"], "failed")
        self.assertEqual(state["latest_success_generation"], 1)

    def test_file_detail_distinguishes_current_and_historical_sources(self):
        """Explains whether each covered line came from current or inherited evidence."""

        report = self.fixture.store.get_report(self.fixture.cycle_id)
        detail = self.fixture.store.get_file(
            self.fixture.cycle_id, report["files"][0]["file_id"]
        )
        sources = {line["new_line_number"]: line["coverage_source"] for line in detail["lines"]}
        self.assertEqual(sources[10], "current_build")
        self.assertEqual(sources[11], "historical_inheritance")
        self.assertEqual(sources[12], "none")

    def test_unknown_opaque_file_id_is_rejected(self):
        """Never treats a caller value as a filesystem path."""

        with self.assertRaises(CoverageReportError) as raised:
            self.fixture.store.get_file(self.fixture.cycle_id, "f" * 64)
        self.assertEqual(raised.exception.code, "report_file_not_found")

    def test_tampered_completion_output_is_rejected(self):
        """Refuses changed line bytes after immutable publication."""

        with (self.fixture.cycle_directory / "cumulative-line-coverage.json").open("a") as output:
            output.write(" ")
        with self.assertRaises(CoverageReportError) as raised:
            self.fixture.store.get_report(self.fixture.cycle_id)
        self.assertEqual(raised.exception.code, "report_completion_invalid")

    def test_request_controlled_output_directory_is_rejected(self):
        """Requires the fixed Cycle/build/input directory derived from persisted identities."""

        results_path = self.fixture.orchestration_directory / "results.json"
        results = json.loads(results_path.read_text())
        results["cycle_coverage_result"]["output_directory"] = "/tmp/not-allowed"
        self.fixture._write_json(results_path, results)
        self.fixture._refresh_orchestration_completion()
        with self.assertRaises(CoverageReportError) as raised:
            self.fixture.store.get_report(self.fixture.cycle_id)
        self.assertEqual(raised.exception.code, "report_path_invalid")

    def test_cycle_without_success_has_no_report(self):
        """Does not invent a report before one immutable analysis succeeds."""

        other_cycle = str(uuid.uuid4())
        with sqlite3.connect(str(self.fixture.database)) as connection:
            connection.execute("INSERT INTO coverage_cycles VALUES (?)", (other_cycle,))
        with self.assertRaises(CoverageReportError) as raised:
            self.fixture.store.get_report(other_cycle)
        self.assertEqual(raised.exception.code, "report_not_available")

    def test_latest_report_read_retries_once_after_generation_advances(self):
        """Reloads a newer immutable report if cleanup removes the stale selection."""

        current = self.fixture.state_store.get_state(self.fixture.cycle_id)
        stale = dict(current)
        stale["latest_success_generation"] = 0
        stale["latest_success_orchestration_sha256"] = "c" * 64
        stale["latest_success_cycle_input_sha256"] = "d" * 64
        stale["latest_success_current_build_id"] = str(uuid.uuid4())

        class AdvancingStateStore:
            def __init__(inner_self):
                inner_self.calls = 0

            def get_state(inner_self, cycle_id):
                inner_self.calls += 1
                return stale if inner_self.calls == 1 else current

        state_store = AdvancingStateStore()
        store = CoverageReportStore(self.fixture.analysis, state_store)
        report = store.get_report(self.fixture.cycle_id)
        self.assertEqual(report["generation"], current["latest_success_generation"])
        self.assertEqual(state_store.calls, 2)


class CoverageReportHTTPTests(unittest.TestCase):
    """Verifies browser shell, Bearer data boundary, and route safety."""

    anonymous_read = False

    def setUp(self):
        """Starts a temporary local HTTP server around one report."""

        self.fixture = ReportFixture()
        self.token = "report-test-token"
        logger = logging.getLogger("report-ui-test-" + uuid.uuid4().hex)
        logger.handlers = [logging.NullHandler()]
        logger.propagate = False
        config = ProbeConfig(
            token=self.token,
            anonymous_read=self.anonymous_read,
            data_dir=self.fixture.root / "data",
            max_file_bytes=1024,
            max_run_bytes=1024,
            max_build_artifact_bytes=1024,
            instance_id=str(uuid.uuid4()),
            logger=logger,
            coverage_store=SimpleNamespace(),
            analysis_state_store=self.fixture.state_store,
            analysis_scheduler=None,
            report_store=self.fixture.store,
        )
        self.server = ProbeHTTPServer(("127.0.0.1", 0), config)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        """Stops the test server and deletes its report."""

        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)
        self.fixture.close()

    def test_html_shell_contains_filters_refresh_and_no_report_data(self):
        """Serves UI behavior publicly while keeping coverage JSON protected."""

        status, headers, body = self._request("GET", "/cycles/" + self.fixture.cycle_id)
        text = body.decode()
        self.assertEqual(status, 200)
        self.assertIn("只看未覆盖代码", text)
        self.assertIn("setTimeout(refresh, 2000)", text)
        self.assertNotIn("sessionStorage", text)
        self.assertNotIn("ios-coverage-operator-token", text)
        self.assertNotIn("Authorization", text)
        self.assertNotIn(self.fixture.path, text)
        self.assertIn("frame-ancestors 'none'", dict(headers)["Content-Security-Policy"])

    def test_report_json_requires_token_and_returns_safe_file_ids(self):
        """Keeps summary/file data behind the existing Bearer boundary."""

        path = "/api/v1/cycles/{}/report".format(self.fixture.cycle_id)
        self.assertEqual(self._request("GET", path)[0], 401)
        status, _, body = self._request("GET", path, token=self.token)
        payload = json.loads(body)
        self.assertEqual(status, 200)
        self.assertEqual(payload["summary"]["uncovered"], 1)
        self.assertRegex(payload["files"][0]["file_id"], r"^[0-9a-f]{64}$")
        self.assertNotIn(str(self.fixture.root), body.decode())

    def test_file_endpoint_uses_only_opaque_identity(self):
        """Returns changed lines by file ID and rejects path-shaped routes."""

        report = self.fixture.store.get_report(self.fixture.cycle_id)
        file_id = report["files"][0]["file_id"]
        prefix = "/api/v1/cycles/{}/report/files/".format(self.fixture.cycle_id)
        status, _, body = self._request("GET", prefix + file_id, token=self.token)
        self.assertEqual(status, 200)
        self.assertEqual(len(json.loads(body)["lines"]), 5)
        self.assertEqual(self._request("GET", prefix + "..%2F..%2Fetc%2Fpasswd", token=self.token)[0], 404)

    def test_analysis_status_exposes_refresh_timestamps(self):
        """Provides request/start/completion times for automatic browser refresh."""

        path = "/api/v1/cycles/{}/analysis".format(self.fixture.cycle_id)
        status, _, body = self._request("GET", path, token=self.token)
        payload = json.loads(body)
        self.assertEqual(status, 200)
        self.assertIsNotNone(payload["requested_at"])
        self.assertIsNotNone(payload["started_at"])
        self.assertIsNotNone(payload["completed_at"])

    def test_invalid_cycle_page_is_rejected_without_echoing_paths(self):
        """Accepts only canonical Cycle UUIDs in the browser route."""

        status, _, body = self._request("GET", "/cycles/not-a-cycle")
        self.assertEqual(status, 400)
        self.assertNotIn("/tmp", body.decode())

    def _request(self, method, path, token=None):
        """Performs one isolated local HTTP request."""

        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=3)
        headers = {"Authorization": "Bearer " + token} if token is not None else {}
        connection.request(method, path, headers=headers)
        response = connection.getresponse()
        body = response.read()
        result = (response.status, response.getheaders(), body)
        connection.close()
        return result


class CoverageReportAnonymousReadHTTPTests(CoverageReportHTTPTests):
    """Verifies anonymous read-only report access when the flag is enabled."""

    anonymous_read = True

    def test_report_json_requires_token_and_returns_safe_file_ids(self):
        """Serves report JSON without any credential while keeping safe file IDs."""

        path = "/api/v1/cycles/{}/report".format(self.fixture.cycle_id)
        status, _, body = self._request("GET", path)
        self.assertEqual(status, 200)
        payload = json.loads(body)
        self.assertEqual(payload["summary"]["uncovered"], 1)
        self.assertRegex(payload["files"][0]["file_id"], r"^[0-9a-f]{64}$")
        self.assertNotIn(str(self.fixture.root), body.decode())

    def test_file_endpoint_allows_anonymous_read(self):
        """Returns changed lines by opaque file ID without a credential."""

        report = self.fixture.store.get_report(self.fixture.cycle_id)
        file_id = report["files"][0]["file_id"]
        prefix = "/api/v1/cycles/{}/report/files/".format(self.fixture.cycle_id)
        status, _, body = self._request("GET", prefix + file_id)
        self.assertEqual(status, 200)
        self.assertEqual(len(json.loads(body)["lines"]), 5)


class CoverageReportWebTests(unittest.TestCase):
    """Verifies the shell never interpolates arbitrary Cycle text."""

    def test_renderer_requires_canonical_cycle_uuid(self):
        """Rejects noncanonical and non-UUID route values."""

        with self.assertRaises((ValueError, AttributeError)):
            render_coverage_report_page("../../etc/passwd")


if __name__ == "__main__":
    unittest.main()
