#!/usr/bin/env python3
"""Tests for the fixed-host read-only GitLab MR boundary."""

import json
import tempfile
import unittest
import subprocess
from pathlib import Path
from urllib.parse import urlsplit

from ioscoverage.integrations.gitlab_mr import (
    GitLabMRClient,
    GitLabMRClientError,
    _NoRedirectHandler,
    normalize_mr_url,
)
from tests.helpers import project_registry


class GitLabMRClientTests(unittest.TestCase):
    """Exercises URL restrictions, fixed requests, and safe upstream failures."""

    def setUp(self) -> None:
        """Creates one isolated credential file and valid MR response."""

        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)
        self.repository = self.root / "repository"
        subprocess.run(["git", "init", "-q", str(self.repository)], check=True)
        self.registry = project_registry(
            self.repository, host="git.example.com", project_path="mobile/example-app"
        )
        self.token = "gitlab-test-token-value"
        self.token_path = self.root / "gitlab_api_token"
        self.token_path.write_text(self.token + "\n", encoding="utf-8")
        self.payload = {
            "iid": 42,
            "web_url": "https://git.example.com/mobile/example-app/merge_requests/42",
            "state": "opened",
            "source_branch": "feat/code_coverage",
            "target_branch": "master",
            "diff_refs": {
                "base_sha": "a" * 40,
                "start_sha": "b" * 40,
                "head_sha": "c" * 40,
            },
        }

    def tearDown(self) -> None:
        """Removes the isolated credential and response fixtures."""

        self.temporary_directory.cleanup()

    def test_detail_diffs_commits_and_query_fragment_normalize(self) -> None:
        """Accepts only the three documented MR page shapes and ignores URL decorations."""

        base = "https://git.example.com/mobile/example-app/merge_requests/42"
        values = (
            base,
            base + "/diffs",
            base + "/commits",
            base + "/diffs?view=parallel#note_1",
            "https://git.example.com/mobile/example-app/-/merge_requests/42",
            "https://git.example.com/mobile/example-app/-/merge_requests/42/diffs",
            "https://git.example.com/mobile/example-app/-/merge_requests/42/commits",
        )
        for value in values:
            with self.subTest(value=value):
                normalized = normalize_mr_url(value, self.registry)
                self.assertEqual(normalized.iid, 42)
                self.assertEqual(normalized.project.project, "ExampleApp")

    def test_url_bypass_shapes_are_rejected(self) -> None:
        """Rejects non-HTTPS, host/project/path drift, credentials, ports, and traversal."""

        invalid = (
            "http://git.example.com/mobile/example-app/merge_requests/42",
            "https://example.com/mobile/example-app/merge_requests/42",
            "https://git.example.com/mobile/Other/merge_requests/42",
            "https://git.example.com/mobile/example-app/issues/42",
            "https://user@git.example.com/mobile/example-app/merge_requests/42",
            "https://git.example.com:443/mobile/example-app/merge_requests/42",
            "https://git.example.com/mobile/example-app/merge_requests/0",
            "https://git.example.com/mobile/example-app/merge_requests/42/../43",
        )
        for value in invalid:
            with self.subTest(value=value), self.assertRaises(GitLabMRClientError) as raised:
                normalize_mr_url(value, self.registry)
            self.assertIn(raised.exception.code, ("invalid_mr_url", "project_not_registered"))

    def test_request_is_fixed_read_only_get_with_private_token(self) -> None:
        """Proves callers cannot control host, path, method, or request headers."""

        captured = {}

        def transport(request, timeout):
            """Captures the standard-library request without performing network I/O."""

            captured["request"] = request
            captured["timeout"] = timeout
            return 200, json.dumps(self.payload).encode("utf-8")

        result = GitLabMRClient(self.token_path, self.registry, transport=transport).fetch(
            "https://git.example.com/mobile/example-app/merge_requests/42/diffs"
        )
        request = captured["request"]
        parsed = urlsplit(request.full_url)
        self.assertEqual(request.get_method(), "GET")
        self.assertEqual(parsed.scheme, "https")
        self.assertEqual(parsed.netloc, "git.example.com")
        self.assertEqual(
            parsed.path, "/api/v4/projects/mobile%2Fexample-app/merge_requests/42"
        )
        self.assertEqual(request.get_header("Private-token"), self.token)
        self.assertEqual(result.target_branch, "master")
        self.assertEqual(result.base_sha, "a" * 40)

    def test_redirects_are_never_followed(self) -> None:
        """Prevents an upstream redirect from forwarding the PAT beyond the fixed host."""

        handler = _NoRedirectHandler()
        self.assertIsNone(
            handler.redirect_request(None, None, 302, "Found", {}, "https://example.com/")
        )

    def test_missing_credential_fails_only_the_lookup(self) -> None:
        """Returns a safe credential error without constructing an HTTP request."""

        client = GitLabMRClient(self.root / "missing", self.registry, transport=lambda request, timeout: (200, b"{}"))
        with self.assertRaises(GitLabMRClientError) as raised:
            client.fetch(
                "https://git.example.com/mobile/example-app/merge_requests/42"
            )
        self.assertEqual(raised.exception.code, "gitlab_token_unavailable")
        self.assertNotIn(self.token, str(raised.exception))

    def test_upstream_status_timeout_and_invalid_json_are_stable(self) -> None:
        """Maps auth, missing MR, timeout, and malformed data to bounded errors."""

        cases = (
            ((401, b"secret upstream body"), "gitlab_auth_failed"),
            ((403, b"secret upstream body"), "gitlab_auth_failed"),
            ((404, b"secret upstream body"), "mr_not_found"),
            ((500, b"secret upstream body"), "gitlab_request_failed"),
            ((200, b"not-json"), "gitlab_invalid_response"),
        )
        url = "https://git.example.com/mobile/example-app/merge_requests/42"
        for response, code in cases:
            with self.subTest(code=code), self.assertRaises(GitLabMRClientError) as raised:
                GitLabMRClient(
                    self.token_path, self.registry,
                    transport=lambda request, timeout, value=response: value,
                ).fetch(url)
            self.assertEqual(raised.exception.code, code)
            self.assertNotIn("secret upstream body", str(raised.exception))

        def timeout_transport(request, timeout):
            """Simulates one bounded socket timeout."""

            raise TimeoutError()

        with self.assertRaises(GitLabMRClientError) as raised:
            GitLabMRClient(self.token_path, self.registry, transport=timeout_transport).fetch(url)
        self.assertEqual(raised.exception.code, "gitlab_timeout")

    def test_required_identity_state_and_diff_refs_are_validated(self) -> None:
        """Rejects project drift, closed MRs, absent fields, and unready diff refs."""

        mutations = (
            ({"iid": 57}, "gitlab_project_mismatch"),
            ({"web_url": "https://git.example.com/other/x/merge_requests/42"}, "gitlab_project_mismatch"),
            ({"state": "closed"}, "mr_not_open"),
            ({"target_branch": ""}, "gitlab_invalid_response"),
            ({"diff_refs": None}, "mr_diff_refs_not_ready"),
            ({"diff_refs": {"base_sha": "a" * 40, "start_sha": "b" * 40}}, "mr_diff_refs_not_ready"),
        )
        url = "https://git.example.com/mobile/example-app/merge_requests/42"
        for changes, code in mutations:
            payload = dict(self.payload)
            payload.update(changes)
            with self.subTest(code=code), self.assertRaises(GitLabMRClientError) as raised:
                GitLabMRClient(
                    self.token_path, self.registry,
                    transport=lambda request, timeout, value=payload: (
                        200,
                        json.dumps(value).encode("utf-8"),
                    ),
                ).fetch(url)
            self.assertEqual(raised.exception.code, code)


if __name__ == "__main__":
    unittest.main()
