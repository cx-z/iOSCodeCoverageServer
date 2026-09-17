#!/usr/bin/env python3
"""Read-only GitLab MR coverage resolver and HTTP boundary tests."""

import http.client
import json
import logging
import sqlite3
import subprocess
import tempfile
import threading
import unittest
import uuid
from pathlib import Path
from types import SimpleNamespace

from ioscoverage.integrations.gitlab_mr import GitLabMRClientError, GitLabMergeRequest
from ioscoverage.server.config import MAX_JSON_BYTES, ProbeConfig
from ioscoverage.server.http.server import ProbeHTTPServer
from ioscoverage.server.mr_coverage_resolver import MergeRequestCoverageResolver
from ioscoverage.storage.coverage_store import CoverageStore
from ioscoverage.storage.reports import CoverageReportError
from tests.helpers import project_registry
from tests.web.test_report_ui import ReportFixture


MR_URL = "https://git.example.com/mobile/example-app/-/merge_requests/42/diffs"
BASE_SHA = "a" * 40
START_SHA = "b" * 40
HEAD_SHA = "c" * 40


class FakeGitLabClient:
    """Provides validated MR identities without network or credential access."""

    def __init__(self, registry):
        self.project_registry = registry
        self.error = None
        self.calls = []
        self.merge_request = GitLabMergeRequest(
            project="ExampleApp",
            project_display_name="Example App",
            iid=42,
            web_url="https://git.example.com/mobile/example-app/merge_requests/42",
            state="opened",
            source_branch="feature/coverage",
            target_branch="main",
            base_sha=BASE_SHA,
            start_sha=START_SHA,
            head_sha=HEAD_SHA,
        )

    def fetch(self, mr_url):
        """Returns one configured response or bounded integration error."""

        self.calls.append(mr_url)
        if self.error is not None:
            raise self.error
        return self.merge_request


class FakeCoverageStore:
    """Exposes only resolver read operations and records their use."""

    def __init__(self, cycle_id, build_id):
        self.calls = []
        self.cycle = {
            "id": cycle_id,
            "project": "ExampleApp",
            "mr_iid": "42",
        }
        self.build = {
            "id": build_id,
            "cycle_id": cycle_id,
            "artifact_state": "ready",
            "merge_base_sha": BASE_SHA,
            "target_sha_at_build": START_SHA,
            "git_sha": HEAD_SHA,
        }

    def find_cycle_by_merge_request(self, project, mr_iid):
        """Returns one matching Cycle without creating it."""

        self.calls.append(("find_cycle", project, str(mr_iid)))
        if self.cycle is None:
            return None
        return dict(self.cycle)

    def get_build(self, build_id):
        """Returns one immutable Build snapshot."""

        self.calls.append(("get_build", build_id))
        return dict(self.build)


class FakeAnalysisStateStore:
    """Returns a configurable scheduler snapshot without mutation methods."""

    def __init__(self, state):
        self.state = state
        self.calls = []

    def get_state(self, cycle_id):
        """Returns a copy of the current read-only state."""

        self.calls.append(cycle_id)
        return dict(self.state)


class FakeReportStore:
    """Returns a configurable completion-verified report projection."""

    def __init__(self, report):
        self.report = report
        self.error = None
        self.calls = []

    def get_report(self, cycle_id):
        """Returns one report or a configured integrity error."""

        self.calls.append(cycle_id)
        if self.error is not None:
            raise self.error
        return json.loads(json.dumps(self.report))


class MergeRequestCoverageResolverTests(unittest.TestCase):
    """Exercises state precedence and exact three-SHA annotation decisions."""

    def setUp(self):
        """Creates one registered fictional project and successful report identity."""

        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        repository = self.root / "ExampleApp"
        subprocess.run(
            ["git", "init", "-q", str(repository)],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        self.registry = project_registry(
            repository,
            host="git.example.com",
            project_path="mobile/example-app",
        )
        self.cycle_id = str(uuid.uuid4())
        self.build_id = str(uuid.uuid4())
        self.gitlab = FakeGitLabClient(self.registry)
        self.coverage = FakeCoverageStore(self.cycle_id, self.build_id)
        self.state = self._state("succeeded", latest=True)
        self.state_store = FakeAnalysisStateStore(self.state)
        self.report = self._report()
        self.report_store = FakeReportStore(self.report)
        self.resolver = MergeRequestCoverageResolver(
            self.gitlab, self.coverage, self.state_store, self.report_store
        )

    def tearDown(self):
        """Removes temporary Git and fixture state."""

        self.temporary.cleanup()

    def test_ready_requires_all_three_exact_build_diff_refs(self):
        """Allows annotations only when base, target-at-build, and head all match."""

        result = self.resolver.resolve(MR_URL)
        self.assertEqual(
            (result["status"], result["can_annotate"], result["should_poll"]),
            ("ready", True, False),
        )
        self.assertEqual(result["report"]["build_diff"]["base_sha"], BASE_SHA)
        self.assertEqual(result["report"]["build_diff"]["start_sha"], START_SHA)
        self.assertEqual(result["report"]["build_diff"]["head_sha"], HEAD_SHA)
        self.assertEqual(
            result["report"]["summary_api"],
            "/api/v1/cycles/{}/report".format(self.cycle_id),
        )

    def test_each_independent_sha_mismatch_is_stale(self):
        """Rejects base-only, start-only, and head-only drift without guessing."""

        field_pairs = (
            ("merge_base_sha", "base_sha"),
            ("target_sha_at_build", "start_sha"),
            ("git_sha", "head_sha"),
        )
        for build_field, response_field in field_pairs:
            with self.subTest(field=build_field):
                self.coverage.build[build_field] = "d" * 40
                result = self.resolver.resolve(MR_URL)
                self.assertEqual(result["status"], "stale_diff")
                self.assertFalse(result["can_annotate"])
                self.assertEqual(result["report"]["build_diff"][response_field], "d" * 40)
                self.coverage.build[build_field] = {
                    "merge_base_sha": BASE_SHA,
                    "target_sha_at_build": START_SHA,
                    "git_sha": HEAD_SHA,
                }[build_field]

    def test_cycle_not_found_never_creates_a_cycle(self):
        """Returns a product status through the store's read-only lookup only."""

        self.coverage.cycle = None
        result = self.resolver.resolve(MR_URL)
        self.assertEqual(result["status"], "cycle_not_found")
        self.assertIsNone(result["cycle"])
        self.assertIsNone(result["analysis"])
        self.assertIsNone(result["report"])
        self.assertEqual(self.coverage.calls, [("find_cycle", "ExampleApp", "42")])

    def test_no_success_maps_idle_pending_and_failed_states(self):
        """Keeps the report null while projecting every scheduler product state."""

        cases = (
            ("idle", "report_not_available", False),
            ("succeeded", "report_not_available", False),
            ("queued", "analysis_pending", True),
            ("running", "analysis_pending", True),
            ("failed", "analysis_failed", False),
        )
        for scheduler_status, expected_status, should_poll in cases:
            with self.subTest(status=scheduler_status):
                self.state_store.state = self._state(scheduler_status, latest=False)
                result = self.resolver.resolve(MR_URL)
                self.assertEqual(result["status"], expected_status)
                self.assertFalse(result["can_annotate"])
                self.assertEqual(result["should_poll"], should_poll)
                self.assertIsNone(result["report"])

    def test_pending_with_matching_latest_success_remains_annotatable(self):
        """Serves the complete prior report while requesting finite polling."""

        for scheduler_status in ("queued", "running"):
            with self.subTest(status=scheduler_status):
                self.state_store.state = self._state(scheduler_status, latest=True)
                result = self.resolver.resolve(MR_URL)
                self.assertEqual(result["status"], "analysis_pending")
                self.assertTrue(result["can_annotate"])
                self.assertTrue(result["should_poll"])
                self.assertIsNotNone(result["report"])

    def test_failed_with_matching_latest_success_remains_annotatable(self):
        """Retains the prior complete report while exposing the latest failure state."""

        self.state_store.state = self._state("failed", latest=True)
        result = self.resolver.resolve(MR_URL)
        self.assertEqual(result["status"], "analysis_failed")
        self.assertTrue(result["can_annotate"])
        self.assertFalse(result["should_poll"])

    def test_stale_diff_has_priority_over_pending_but_still_polls(self):
        """Never annotates an old diff while allowing the browser to await new output."""

        self.state_store.state = self._state("running", latest=True)
        self.coverage.build["git_sha"] = "d" * 40
        result = self.resolver.resolve(MR_URL)
        self.assertEqual(result["status"], "stale_diff")
        self.assertFalse(result["can_annotate"])
        self.assertTrue(result["should_poll"])

    def test_non_open_and_unready_refs_are_stable_product_states(self):
        """Maps valid registered MR product states without touching Cycle storage."""

        for code in ("mr_not_open", "mr_diff_refs_not_ready"):
            with self.subTest(code=code):
                self.gitlab.error = GitLabMRClientError(code, "safe", 409)
                result = self.resolver.resolve(MR_URL)
                self.assertEqual(result["status"], code)
                self.assertFalse(result["can_annotate"])
                self.assertEqual(
                    result["mr"]["diff"],
                    {"base_sha": None, "start_sha": None, "head_sha": None},
                )
                self.assertEqual(self.coverage.calls, [])
                self.gitlab.error = None

    def test_other_gitlab_errors_remain_http_errors(self):
        """Does not disguise credentials, timeout, or malformed upstream data as product state."""

        self.gitlab.error = GitLabMRClientError(
            "gitlab_auth_failed", "GitLab credential was rejected", 502
        )
        with self.assertRaises(GitLabMRClientError) as raised:
            self.resolver.resolve(MR_URL)
        self.assertEqual(raised.exception.code, "gitlab_auth_failed")

    def test_report_state_build_and_completion_identity_drift_are_rejected(self):
        """Rejects mismatched state projection, Build ownership, and report integrity errors."""

        mutations = (
            ("report_generation", lambda: self.report_store.report.update(generation=8)),
            ("build_cycle", lambda: self.coverage.build.update(cycle_id=str(uuid.uuid4()))),
            ("build_state", lambda: self.coverage.build.update(artifact_state="invalid")),
        )
        for name, mutate in mutations:
            with self.subTest(name=name):
                self.report_store.report = self._report()
                self.coverage.build.update(
                    cycle_id=self.cycle_id,
                    artifact_state="ready",
                )
                mutate()
                with self.assertRaises(CoverageReportError) as raised:
                    self.resolver.resolve(MR_URL)
                self.assertEqual(raised.exception.code, "report_identity_mismatch")

        self.report_store.report = self._report()
        self.report_store.error = CoverageReportError(
            "report_completion_invalid", "Coverage completion is invalid"
        )
        with self.assertRaises(CoverageReportError) as raised:
            self.resolver.resolve(MR_URL)
        self.assertEqual(raised.exception.code, "report_completion_invalid")

    def test_response_projection_excludes_paths_tokens_and_internal_evidence(self):
        """Returns only browser-safe identity, summary, and relative API fields."""

        result_text = json.dumps(self.resolver.resolve(MR_URL), sort_keys=True)
        for forbidden in (
            str(self.root),
            "PRIVATE-TOKEN",
            "output_directory",
            "profraw",
            "dSYM",
            "Mach-O",
        ):
            self.assertNotIn(forbidden, result_text)

    def _state(self, status, latest):
        """Builds one production-shaped scheduler state fixture."""

        return {
            "cycle_id": self.cycle_id,
            "status": status,
            "requested_generation": 7,
            "completed_generation": 7,
            "latest_success_generation": 7 if latest else None,
            "latest_success_current_build_id": self.build_id if latest else None,
            "latest_success_orchestration_sha256": "d" * 64 if latest else None,
            "latest_success_cycle_input_sha256": "e" * 64 if latest else None,
        }

    def _report(self):
        """Builds one safe latest-success report projection."""

        return {
            "schema_version": 1,
            "cycle_id": self.cycle_id,
            "generation": 7,
            "updated_at": "2026-09-09T00:00:00+00:00",
            "current_build_id": self.build_id,
            "orchestration_input_sha256": "d" * 64,
            "cycle_input_set_sha256": "e" * 64,
            "summary": {
                "changed_new_side": 43,
                "measurable": 35,
                "covered": 30,
                "uncovered": 5,
                "not_measurable": 8,
                "unknown": 0,
                "changed_line_coverage_percent": 85.7142857143,
            },
        }


class ResolverReadOnlyStorageTests(unittest.TestCase):
    """Checks stable Cycle lookup and resolver reads against real temporary persistence."""

    def test_cycle_lookup_and_full_resolver_leave_database_and_files_unchanged(self):
        """Proves the resolver reads state/results without inserts, scheduling, or files."""

        fixture = ReportFixture()
        temporary = tempfile.TemporaryDirectory()
        try:
            root = Path(temporary.name)
            repository = root / "ExampleApp"
            subprocess.run(["git", "init", "-q", str(repository)], check=True)
            registry = project_registry(
                repository,
                host="git.example.com",
                project_path="mobile/example-app",
            )
            store = CoverageStore(
                root / "coverage.sqlite3",
                root / "data",
                registry,
                SimpleNamespace(),
            )
            cycle = store.create_or_get_cycle(
                {
                    "schema_version": 1,
                    "project": "ExampleApp",
                    "mr_iid": "42",
                    "mr_url": "https://git.example.com/mobile/example-app/merge_requests/42",
                    "target_branch": "main",
                    "base_sha": BASE_SHA,
                    "head_sha": HEAD_SHA,
                }
            )[0]
            before = (root / "coverage.sqlite3").read_bytes()
            found = store.find_cycle_by_merge_request("ExampleApp", 42)
            after = (root / "coverage.sqlite3").read_bytes()
            self.assertEqual(found["id"], cycle["id"])
            self.assertEqual(before, after)

            coverage = FakeCoverageStore(fixture.cycle_id, fixture.build_id)
            gitlab = FakeGitLabClient(registry)
            resolver = MergeRequestCoverageResolver(
                gitlab, coverage, fixture.state_store, fixture.store
            )
            database_rows_before = self._database_dump(fixture.database)
            files_before = self._file_identities(fixture.root)
            result = resolver.resolve(MR_URL)
            self.assertEqual(result["status"], "ready")
            self.assertEqual(database_rows_before, self._database_dump(fixture.database))
            self.assertEqual(files_before, self._file_identities(fixture.root))
        finally:
            temporary.cleanup()
            fixture.close()

    def _database_dump(self, path):
        """Returns a logical SQLite snapshot without exposing deployment data."""

        with sqlite3.connect(str(path)) as connection:
            return tuple(connection.iterdump())

    def _file_identities(self, root):
        """Returns stable relative file sizes and bytes for one temporary fixture."""

        return {
            path.relative_to(root).as_posix(): path.read_bytes()
            for path in root.rglob("*")
            if path.is_file()
        }


class GitLabCoverageResolverHTTPTests(unittest.TestCase):
    """Verifies strict JSON and operator-only authentication on the new route."""

    anonymous_read = False

    def setUp(self):
        """Starts one isolated route with a deterministic resolver collaborator."""

        self.operator_token = "operator-resolver-test-token"
        self.run_token = "run-resolver-test-token"
        self.resolver = SimpleNamespace(
            resolve=lambda mr_url: {
                "schema_version": 1,
                "status": "cycle_not_found",
                "can_annotate": False,
                "should_poll": False,
                "mr": {
                    "host": "git.example.com",
                    "project_path": "mobile/example-app",
                    "iid": "42",
                    "canonical_url": "https://git.example.com/mobile/example-app/merge_requests/42",
                    "diff": {
                        "base_sha": BASE_SHA,
                        "start_sha": START_SHA,
                        "head_sha": HEAD_SHA,
                    },
                },
                "cycle": None,
                "analysis": None,
                "report": None,
            }
        )
        logger = logging.getLogger("resolver-http-test-" + uuid.uuid4().hex)
        logger.handlers = [logging.NullHandler()]
        logger.propagate = False
        config = ProbeConfig(
            token=self.operator_token,
            run_upload_token=self.run_token,
            anonymous_read=self.anonymous_read,
            data_dir=Path(tempfile.gettempdir()) / "ios-coverage-resolver-test",
            max_file_bytes=1024,
            max_run_bytes=1024,
            max_build_artifact_bytes=1024,
            instance_id=str(uuid.uuid4()),
            logger=logger,
            coverage_store=SimpleNamespace(),
            analysis_state_store=SimpleNamespace(),
            analysis_scheduler=None,
            report_store=None,
            merge_request_coverage_resolver=self.resolver,
        )
        self.server = ProbeHTTPServer(("127.0.0.1", 0), config)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        """Stops the isolated HTTP service."""

        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)

    def test_only_operator_token_can_resolve(self):
        """Returns 401 for unknown credentials and 403 for the restricted Run token."""

        self.assertEqual(self._request(None)[0], 401)
        self.assertEqual(self._request("wrong-token")[0], 401)
        self.assertEqual(self._request(self.run_token)[0], 403)
        status, body = self._request(self.operator_token)
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["status"], "cycle_not_found")

    def test_body_schema_unknown_fields_and_size_are_strict(self):
        """Rejects missing, additional, wrong-version, empty, and oversized JSON bodies."""

        cases = (
            ({"schema_version": 1}, 400),
            ({"schema_version": 1, "mr_url": MR_URL, "path": "/tmp/x"}, 400),
            ({"schema_version": True, "mr_url": MR_URL}, 400),
            ({"schema_version": 2, "mr_url": MR_URL}, 400),
        )
        for payload, expected in cases:
            with self.subTest(payload=payload):
                self.assertEqual(self._request(self.operator_token, payload)[0], expected)
        self.assertEqual(self._request_raw(self.operator_token, b"")[0], 413)
        self.assertEqual(
            self._request_raw(
                self.operator_token,
                b"{}",
                declared_length=MAX_JSON_BYTES + 1,
            )[0],
            413,
        )

    def test_upstream_error_is_safe_and_does_not_echo_request_url_or_token(self):
        """Maps bounded GitLab failures without reflecting credentials or MR input."""

        def fail(_mr_url):
            raise GitLabMRClientError(
                "gitlab_auth_failed", "GitLab credential was rejected", 502
            )

        self.resolver.resolve = fail
        status, body = self._request(self.operator_token)
        text = body.decode("utf-8")
        self.assertEqual(status, 502)
        self.assertEqual(json.loads(body)["error"], "gitlab_auth_failed")
        self.assertNotIn(self.operator_token, text)
        self.assertNotIn(MR_URL, text)

    def _request(self, token, payload=None):
        """Sends one valid resolver request unless a test supplies another payload."""

        if payload is None:
            payload = {"schema_version": 1, "mr_url": MR_URL}
        body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        return self._request_raw(token, body)

    def _request_raw(self, token, body, declared_length=None):
        """Sends exact request bytes with a controllable declared size."""

        connection = http.client.HTTPConnection(
            "127.0.0.1", self.server.server_port, timeout=3
        )
        headers = {
            "Content-Type": "application/json",
            "Content-Length": str(
                len(body) if declared_length is None else declared_length
            ),
        }
        if token is not None:
            headers["Authorization"] = "Bearer " + token
        connection.request(
            "POST",
            "/api/v1/integrations/gitlab/merge-request-coverage/resolve",
            body=body,
            headers=headers,
        )
        response = connection.getresponse()
        result = (response.status, response.read())
        connection.close()
        return result


class GitLabCoverageResolverAnonymousReadHTTPTests(GitLabCoverageResolverHTTPTests):
    """Verifies the read-only resolver is reachable without a credential when enabled."""

    anonymous_read = True

    def test_only_operator_token_can_resolve(self):
        """Anonymous read allows every credential shape to reach the resolver."""

        for token in (None, "wrong-token", self.run_token):
            status, body = self._request(token)
            self.assertEqual(status, 200)
            self.assertEqual(json.loads(body)["status"], "cycle_not_found")

    def test_anonymous_read_keeps_write_and_list_routes_token_protected(self):
        """Cycle creation and the cycles list still require the operator Token."""

        connection = http.client.HTTPConnection(
            "127.0.0.1", self.server.server_port, timeout=3
        )
        connection.request(
            "POST",
            "/api/v1/cycles",
            body=b"{}",
            headers={"Content-Type": "application/json", "Content-Length": "2"},
        )
        response = connection.getresponse()
        response.read()
        connection.close()
        self.assertEqual(response.status, 401)

        connection = http.client.HTTPConnection(
            "127.0.0.1", self.server.server_port, timeout=3
        )
        connection.request("GET", "/api/v1/cycles")
        response = connection.getresponse()
        response.read()
        connection.close()
        self.assertEqual(response.status, 401)


if __name__ == "__main__":
    unittest.main()
