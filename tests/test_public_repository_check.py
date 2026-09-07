"""Tests for the pre-publication candidate scanner."""

import contextlib
import io
import subprocess
import tempfile
import unittest
from pathlib import Path

from scripts.check_public_repository import main, publication_candidates, scan_repository


class PublicRepositoryCheckTests(unittest.TestCase):
    """Exercises tracked, untracked, ignored, text, and binary candidate handling."""

    def setUp(self) -> None:
        """Creates one empty repository with the production ignore policy."""

        self.temporary = tempfile.TemporaryDirectory(prefix="ios-coverage-tests-public-")
        self.root = Path(self.temporary.name).resolve()
        subprocess.run(["git", "init", "-q", str(self.root)], check=True)
        source_ignore = Path(__file__).resolve().parents[1] / ".gitignore"
        (self.root / ".gitignore").write_bytes(source_ignore.read_bytes())

    def tearDown(self) -> None:
        """Removes the isolated repository."""

        self.temporary.cleanup()

    def test_public_source_with_forbidden_product_name_fails(self) -> None:
        """Finds a legacy product literal assembled only inside this test at runtime."""

        forbidden = "Knock" + "nock"
        (self.root / "module.py").write_text("name = {!r}\n".format(forbidden), encoding="utf-8")
        findings = scan_repository(self.root)
        self.assertIn("legacy_product_name", {item.kind for item in findings})

    def test_example_configuration_with_local_home_path_fails(self) -> None:
        """Rejects machine-specific home directories without embedding a real developer path."""

        config = self.root / "config.json"
        local_path = "/" + "Users" + "/example/Project"
        config.write_text('{"repository_path":' + repr(local_path) + '}\n', encoding="utf-8")
        self.assertIn("local_home_path", {item.kind for item in scan_repository(self.root)})

    def test_ignored_runtime_details_do_not_enter_candidate_set(self) -> None:
        """Excludes private project data below every anchored runtime directory."""

        private_name = "Knock" + "nock"
        paths = (
            self.root / "runtime/config/projects.json",
            self.root / "runtime/state/operator_token",
            self.root / "runtime/data/example.profraw",
        )
        for path in paths:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(private_name, encoding="utf-8")
        candidates = {path.relative_to(self.root).as_posix() for path in publication_candidates(self.root)}
        self.assertTrue(all(path.relative_to(self.root).as_posix() not in candidates for path in paths))
        self.assertEqual(scan_repository(self.root), ())

    def test_ignored_credential_file_is_not_publishable(self) -> None:
        """Confirms a root-local credential filename is excluded by Git ignore policy."""

        token_path = self.root / "state/run_upload_token"
        token_path.parent.mkdir(parents=True)
        token_path.write_text("test-secret", encoding="utf-8")
        self.assertNotIn(token_path, publication_candidates(self.root))

    def test_common_coverage_technologies_are_allowed(self) -> None:
        """Does not reject generic GitLab, LLVM, Xcode, Swift, or iOS terminology."""

        (self.root / "README.md").write_text(
            "GitLab iOS Xcode LLVM Swift coverage\n", encoding="utf-8"
        )
        self.assertEqual(scan_repository(self.root), ())

    def test_binary_secret_is_never_echoed(self) -> None:
        """Reports only a safe category and path when binary bytes contain a credential signature."""

        secret = b"gl" + b"pat-" + b"A" * 24
        (self.root / "fixture.bin").write_bytes(b"\x00\xff" + secret + b"\x00")
        stdout = io.StringIO()
        stderr = io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            result = main(["--repository", str(self.root)])
        combined = stdout.getvalue() + stderr.getvalue()
        self.assertEqual(result, 1)
        self.assertIn("gitlab_access_token", combined)
        self.assertNotIn(secret.decode("ascii"), combined)


if __name__ == "__main__":
    unittest.main()
