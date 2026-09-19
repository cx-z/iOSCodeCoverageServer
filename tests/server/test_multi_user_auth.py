#!/usr/bin/env python3
"""HTTP integration tests for web Sessions and the six-principal permission surface."""

import http.client
import json
import logging
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path

from ioscoverage.integrations.gitlab_mr import GitLabMergeRequest
from ioscoverage.runtime import RuntimePaths
from server import create_server
from tests.helpers import project_registry


class FakeGitLabMRClient:
    """Returns one deterministic open MR snapshot without network access."""

    def fetch(self, _mr_url):
        """Returns the fixed registered-project MR used by submission tests."""

        return GitLabMergeRequest(
            project="ExampleApp",
            project_display_name="Example App",
            iid=42,
            web_url="https://git.example.com/mobile/example-app/merge_requests/42",
            state="opened",
            source_branch="feature",
            target_branch="main",
            base_sha="a" * 40,
            start_sha="b" * 40,
            head_sha="c" * 40,
        )


class MultiUserHTTPTests(unittest.TestCase):
    """Exercises registration, isolation, admin projection, and token scopes."""

    def setUp(self) -> None:
        """Starts one isolated server with distinct machine credentials."""

        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)
        repository = self.root / "ExampleApp"
        subprocess.run(
            ["git", "init", str(repository)],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        self.operator = "operator-token-value"
        self.packaging = "packaging-token-value"
        self.run_upload = "run-upload-token-value"
        logger = logging.getLogger("multi-user-http-" + str(id(self)))
        logger.handlers = [logging.NullHandler()]
        logger.propagate = False
        registry = project_registry(repository)
        gitlab_client = FakeGitLabMRClient()
        gitlab_client.project_registry = registry
        self.server = create_server(
            "127.0.0.1",
            0,
            self.operator,
            RuntimePaths.from_root(self.root),
            registry,
            logger=logger,
            start_analysis_scheduler=False,
            run_upload_token=self.run_upload,
            packaging_token=self.packaging,
            gitlab_mr_client=gitlab_client,
            anonymous_read=True,
        )
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self) -> None:
        """Stops the isolated HTTP server and removes temporary state."""

        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.temporary_directory.cleanup()

    def request(self, method, path, payload=None, token=None, cookie=None, origin=True):
        """Sends one request and returns status, response headers, and decoded JSON."""

        body = None
        headers = {}
        if payload is not None:
            body = json.dumps(payload, separators=(",", ":")).encode()
            headers["Content-Type"] = "application/json"
            headers["Content-Length"] = str(len(body))
        if token is not None:
            headers["Authorization"] = "Bearer " + token
        if cookie is not None:
            headers["Cookie"] = cookie
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=3)
        if origin:
            headers["Origin"] = "http://127.0.0.1:{}".format(self.server.server_port)
        connection.request(method, path, body=body, headers=headers)
        response = connection.getresponse()
        raw = response.read()
        result = (response.status, dict(response.getheaders()), json.loads(raw) if raw else {})
        connection.close()
        return result

    def page_request(self, path, *, cookie=None, token=None):
        """Reads one HTML or redirect response without interpreting it as JSON."""

        headers = {}
        if cookie is not None:
            headers["Cookie"] = cookie
        if token is not None:
            headers["Authorization"] = "Bearer " + token
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=3)
        connection.request("GET", path, headers=headers)
        response = connection.getresponse()
        result = response.status, dict(response.getheaders()), response.read().decode("utf-8")
        connection.close()
        return result

    def register(self, username):
        """Registers one developer and returns its user document and Cookie header."""

        status, headers, body = self.request(
            "POST",
            "/api/v1/auth/register",
            {"schema_version": 1, "username": username, "password": "password-value"},
        )
        self.assertEqual(status, 201)
        return body["user"], headers["Set-Cookie"].split(";", 1)[0]

    def login(self, username, password="password-value"):
        """Authenticates one account and returns its Cookie header."""

        status, headers, body = self.request(
            "POST",
            "/api/v1/auth/login",
            {"schema_version": 1, "username": username, "password": password},
        )
        self.assertEqual(status, 200, body)
        return headers["Set-Cookie"].split(";", 1)[0]

    def submit_mr(self, cookie):
        """Submits the fixed MR as one web user."""

        return self.request(
            "POST",
            "/api/v1/cycles",
            {
                "schema_version": 1,
                "mr_url": "https://git.example.com/mobile/example-app/merge_requests/42",
            },
            cookie=cookie,
        )

    def test_register_login_session_logout_and_cookie_contract(self) -> None:
        """Completes the auth lifecycle with a secret-free HttpOnly Strict Cookie."""

        user, cookie = self.register("developer-a")
        set_cookie = self.request(
            "POST",
            "/api/v1/auth/login",
            {"schema_version": 1, "username": "developer-a", "password": "password-value"},
        )[1]["Set-Cookie"]
        self.assertIn("HttpOnly", set_cookie)
        self.assertIn("SameSite=Strict", set_cookie)
        self.assertIn("Path=/", set_cookie)
        self.assertNotIn("password-value", set_cookie)
        status, _, body = self.request("GET", "/api/v1/auth/session", cookie=cookie)
        self.assertEqual(status, 200)
        self.assertEqual(body["user"]["id"], user["id"])
        status, headers, _ = self.request("POST", "/api/v1/auth/logout", {}, cookie=cookie)
        self.assertEqual(status, 200)
        self.assertIn("Max-Age=0", headers["Set-Cookie"])
        self.assertEqual(self.request("GET", "/api/v1/auth/session", cookie=cookie)[0], 401)

    def test_auth_page_and_cycle_workbench_are_separate(self) -> None:
        """Routes anonymous and authenticated browsers to mutually exclusive shells."""

        status, headers, login = self.page_request("/login/")
        self.assertEqual(status, 200)
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertIn('id="login-form"', login)
        self.assertNotIn('id="create-form"', login)
        status, headers, _ = self.page_request("/cycles/")
        self.assertEqual(status, 302)
        self.assertEqual(headers["Location"], "/login/")
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertEqual(self.page_request("/login")[1]["Location"], "/login/")
        self.assertEqual(self.page_request("/cycles")[1]["Location"], "/cycles/")

        _, cookie = self.register("developer-a")
        status, headers, _ = self.page_request("/login/", cookie=cookie)
        self.assertEqual(status, 302)
        self.assertEqual(headers["Location"], "/cycles/")
        status, _, workbench = self.page_request("/cycles/", cookie=cookie)
        self.assertEqual(status, 200)
        self.assertIn('id="create-form"', workbench)
        self.assertNotIn('id="login-form"', workbench)
        self.assertEqual(self.request("GET", "/api/v1/cycles", cookie=cookie)[0], 200)

        for token in (self.operator, self.packaging, self.run_upload):
            with self.subTest(token_scope=token):
                self.assertEqual(self.page_request("/cycles/", token=token)[0], 302)
        self.assertEqual(self.page_request("/cycles/", cookie=cookie, token="invalid")[0], 302)

        status, _, _ = self.request(
            "POST",
            "/api/v1/admin/users",
            {"schema_version": 1, "username": "admin-user", "password": "password-value", "role": "admin"},
            token=self.operator,
            origin=False,
        )
        self.assertEqual(status, 201)
        admin_cookie = self.login("admin-user")
        self.assertEqual(self.page_request("/login/", cookie=admin_cookie)[1]["Location"], "/cycles/")
        self.assertIn('id="admin-users"', self.page_request("/cycles/", cookie=admin_cookie)[2])

        self.assertEqual(self.request("POST", "/api/v1/auth/logout", {}, cookie=cookie)[0], 200)
        self.assertEqual(self.page_request("/cycles/", cookie=cookie)[1]["Location"], "/login/")
        self.assertEqual(self.page_request("/login/", cookie=cookie)[0], 200)
        self.assertEqual(self.request("GET", "/api/v1/cycles", cookie=cookie)[0], 401)

    def test_developer_lists_are_isolated_and_same_mr_cycle_is_shared(self) -> None:
        """Returns only explicit submissions while preserving project/MR uniqueness."""

        _, cookie_a = self.register("developer-a")
        _, cookie_b = self.register("developer-b")
        first_status, _, first = self.submit_mr(cookie_a)
        self.assertEqual(first_status, 201)
        self.assertEqual(first["submission_status"], "added")
        self.assertEqual(self.request("GET", "/api/v1/cycles", cookie=cookie_b)[2]["cycles"], [])
        second_status, _, second = self.submit_mr(cookie_b)
        third_status, _, third = self.submit_mr(cookie_a)
        self.assertEqual((second_status, third_status), (200, 200))
        self.assertEqual(second["cycle"]["id"], first["cycle"]["id"])
        self.assertEqual(third["submission_status"], "existing")
        self.assertEqual(len(self.request("GET", "/api/v1/cycles", cookie=cookie_a)[2]["cycles"]), 1)
        self.assertEqual(len(self.request("GET", "/api/v1/cycles", cookie=cookie_b)[2]["cycles"]), 1)

    def test_admin_sees_all_submitters_and_manages_session_invalidation(self) -> None:
        """Projects submitter usernames and revokes sessions on disable/reset."""

        developer, developer_cookie = self.register("developer-a")
        self.submit_mr(developer_cookie)
        create_status, _, _ = self.request(
            "POST",
            "/api/v1/admin/users",
            {
                "schema_version": 1,
                "username": "admin-user",
                "password": "password-value",
                "role": "admin",
            },
            token=self.operator,
            origin=False,
        )
        self.assertEqual(create_status, 201)
        admin_cookie = self.login("admin-user")
        cycles = self.request("GET", "/api/v1/cycles", cookie=admin_cookie)[2]["cycles"]
        self.assertEqual([item["username"] for item in cycles[0]["submitters"]], ["developer-a"])
        status, _, _ = self.request(
            "PATCH",
            "/api/v1/admin/users/{}/status".format(developer["id"]),
            {"schema_version": 1, "status": "disabled"},
            cookie=admin_cookie,
        )
        self.assertEqual(status, 200)
        self.assertEqual(self.request("GET", "/api/v1/cycles", cookie=developer_cookie)[0], 401)

    def test_admin_password_reset_revokes_sessions_and_accepts_only_new_password(self) -> None:
        """Keeps password replacement and complete Session invalidation atomic through HTTP."""

        developer, old_cookie = self.register("developer-a")
        self.request(
            "POST",
            "/api/v1/admin/users",
            {
                "schema_version": 1,
                "username": "admin-user",
                "password": "password-value",
                "role": "admin",
            },
            token=self.operator,
            origin=False,
        )
        admin_cookie = self.login("admin-user")
        status, _, _ = self.request(
            "POST",
            "/api/v1/admin/users/{}/reset-password".format(developer["id"]),
            {"schema_version": 1, "password": "replacement-password"},
            cookie=admin_cookie,
        )
        self.assertEqual(status, 200)
        self.assertEqual(self.request("GET", "/api/v1/auth/session", cookie=old_cookie)[0], 401)
        self.assertEqual(
            self.request(
                "POST",
                "/api/v1/auth/login",
                {"schema_version": 1, "username": "developer-a", "password": "password-value"},
            )[2]["error"],
            "invalid_credentials",
        )
        self.assertTrue(self.login("developer-a", "replacement-password").startswith("ios_coverage_session="))

    def test_invalid_bearer_never_falls_back_to_valid_cookie_and_origin_is_required(self) -> None:
        """Fails closed on credential ambiguity and cross-origin Session writes."""

        _, cookie = self.register("developer-a")
        status, _, body = self.request(
            "GET", "/api/v1/cycles", token="invalid-bearer-token", cookie=cookie
        )
        self.assertEqual(status, 401)
        self.assertEqual(body["error"], "invalid_token")
        status, _, body = self.request(
            "POST",
            "/api/v1/cycles",
            {"schema_version": 1, "mr_url": "https://git.example.com/x"},
            cookie=cookie,
            origin=False,
        )
        self.assertEqual(status, 403)
        self.assertEqual(body["error"], "origin_not_allowed")

    def test_packaging_scope_is_limited_to_known_cycle_and_build_operations(self) -> None:
        """Allows the four packaging capabilities while denying enumeration and management."""

        _, cookie = self.register("developer-a")
        cycle = self.submit_mr(cookie)[2]["cycle"]
        self.assertEqual(
            self.request("GET", "/api/v1/cycles/" + cycle["id"], token=self.packaging, origin=False)[0],
            200,
        )
        patch_status = self.request(
            "PATCH",
            "/api/v1/cycles/" + cycle["id"],
            {"schema_version": 1, "head_sha": "d" * 40},
            token=self.packaging,
            origin=False,
        )[0]
        self.assertEqual(patch_status, 200)
        self.assertEqual(
            self.request("GET", "/api/v1/builds/00000000-0000-0000-0000-000000000000", token=self.packaging, origin=False)[0],
            404,
        )
        denied = (
            ("GET", "/api/v1/cycles", None),
            ("POST", "/api/v1/cycles", {"schema_version": 1, "mr_url": "https://git.example.com/x"}),
            ("GET", "/api/v1/admin/users", None),
            ("POST", "/api/v1/cycles/{}/analysis/retry".format(cycle["id"]), None),
            ("POST", "/api/v1/maintenance/cleanup", None),
        )
        for method, path, payload in denied:
            with self.subTest(method=method, path=path):
                self.assertEqual(
                    self.request(method, path, payload, token=self.packaging, origin=False)[0],
                    403,
                )

    def test_six_principal_permission_matrix(self) -> None:
        """Locks account, Cycle, Build, Run, report, retry, and cleanup authorization rows."""

        _, developer_cookie = self.register("developer-a")
        cycle = self.submit_mr(developer_cookie)[2]["cycle"]
        self.request(
            "POST",
            "/api/v1/admin/users",
            {
                "schema_version": 1,
                "username": "admin-user",
                "password": "password-value",
                "role": "admin",
            },
            token=self.operator,
            origin=False,
        )
        admin_cookie = self.login("admin-user")
        identities = {
            "anonymous": {},
            "developer": {"cookie": developer_cookie},
            "admin": {"cookie": admin_cookie},
            "packaging": {"token": self.packaging, "origin": False},
            "run_upload": {"token": self.run_upload, "origin": False},
            "operator": {"token": self.operator, "origin": False},
        }
        expectations = {
            "anonymous": (401, 401, 401, 401, 401),
            "developer": (200, 200, 403, 403, 403),
            "admin": (200, 200, 403, 200, 503),
            "packaging": (403, 200, 404, 403, 403),
            "run_upload": (403, 403, 403, 403, 403),
            "operator": (200, 200, 404, 200, 503),
        }
        for name, credentials in identities.items():
            with self.subTest(principal=name):
                observed = (
                    self.request("GET", "/api/v1/cycles", **credentials)[0],
                    self.request("GET", "/api/v1/cycles/" + cycle["id"], **credentials)[0],
                    self.request(
                        "GET",
                        "/api/v1/builds/00000000-0000-0000-0000-000000000000",
                        **credentials,
                    )[0],
                    self.request("GET", "/api/v1/admin/users", **credentials)[0],
                    self.request("POST", "/api/v1/maintenance/cleanup", **credentials)[0],
                )
                self.assertEqual(observed, expectations[name])
                self.assertEqual(
                    self.request("GET", "/api/v1/cycles/" + cycle["id"] + "/report", **credentials)[0],
                    409,
                )
                self.assertEqual(
                    self.request(
                        "POST",
                        "/api/v1/integrations/gitlab/merge-request-coverage/resolve",
                        {
                            "schema_version": 1,
                            "mr_url": "https://git.example.test/mobile/ExampleApp/merge_requests/42",
                        },
                        **credentials,
                    )[0],
                    200,
                )


if __name__ == "__main__":
    unittest.main()
