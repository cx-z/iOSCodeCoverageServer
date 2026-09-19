#!/usr/bin/env python3
"""Static safety tests for the Session-based Cycle and user-management shells."""

import unittest

from ioscoverage.web.pages.cycles import render_cycle_list_page
from ioscoverage.web.pages.login import render_login_page
from ioscoverage.web.pages.users import render_user_admin_page


class MultiUserWebShellTests(unittest.TestCase):
    """Proves browser shells contain no business data or machine credential handling."""

    def test_login_shell_has_exclusive_forms_and_no_cycle_controls(self) -> None:
        """Defaults to login and submits registration through the existing Session API."""

        text = render_login_page().decode()
        self.assertIn('id="login-form"', text)
        self.assertIn('id="register-form" hidden', text)
        self.assertIn('id="show-register"', text)
        self.assertIn('id="show-login"', text)
        self.assertIn('/api/v1/auth/login', text)
        self.assertIn('/api/v1/auth/register', text)
        self.assertIn("location.assign('/cycles/')", text)
        self.assertNotIn('id="create-form"', text)
        self.assertNotIn('id="cycle-rows"', text)
        self.assertNotIn('/api/v1/cycles', text)
        self.assertNotIn('sessionStorage', text)
        self.assertNotIn('localStorage', text)
        self.assertNotIn('Authorization', text)

    def test_cycle_shell_only_contains_workbench_and_safe_dom_projection(self) -> None:
        """Excludes auth forms and Bearer storage from the logged-in workbench."""

        text = render_cycle_list_page().decode()
        self.assertIn("/api/v1/auth/logout", text)
        self.assertIn("/api/v1/auth/session", text)
        self.assertIn('id="create-form"', text)
        self.assertIn('id="cycle-rows"', text)
        self.assertNotIn('id="login-form"', text)
        self.assertNotIn('id="register-form"', text)
        self.assertNotIn("/api/v1/auth/register", text)
        self.assertNotIn("/api/v1/auth/login", text)
        self.assertIn("textContent", text)
        self.assertNotIn("sessionStorage", text)
        self.assertNotIn("localStorage", text)
        self.assertNotIn("Authorization", text)
        self.assertNotIn("ios-coverage-operator-token", text)

    def test_admin_shell_is_data_free_and_clears_password_fields(self) -> None:
        """Fetches users from same-origin APIs and clears both password inputs after use."""

        text = render_user_admin_page().decode()
        self.assertIn("/api/v1/admin/users", text)
        self.assertIn("payload.user.role !== 'admin'", text)
        self.assertIn("password.value = ''", text)
        self.assertIn("textContent", text)
        self.assertNotIn("sessionStorage", text)
        self.assertNotIn("Authorization", text)
        self.assertNotIn("password_digest", text)


if __name__ == "__main__":
    unittest.main()
