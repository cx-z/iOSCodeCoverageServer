"""Real Git regressions for generic MR repository synchronization."""

import json
import os
import shlex
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from ioscoverage.integrations.git_sync import GitRepositorySynchronizer, RepositorySyncError
from ioscoverage.integrations.gitlab_mr import GitLabMergeRequest
from ioscoverage.projects import GitLabProject
from tests.helpers import project_registry


class GitSyncWorkspace:
    """Keeps a bare origin, developer clone and stale server clone entirely local."""

    def __init__(self, root, name="app-a"):
        """Creates unrelated repository histories without contacting a real GitLab."""

        self.origin = root / (name + ".git")
        self.name = name
        self.developer = root / (name + "-developer")
        self.server = root / (name + "-server")
        self.git(root, "init", "--bare", "-q", str(self.origin))
        self.git(root, "clone", "-q", str(self.origin), str(self.developer))
        self.git(self.developer, "config", "user.name", "Test")
        self.git(self.developer, "config", "user.email", "test@example.test")
        self.git(self.developer, "checkout", "-q", "-b", "main")
        self.base = self.commit("base\n")
        self.git(self.developer, "push", "-q", "origin", "main")
        self.git(root, "clone", "-q", "-b", "main", str(self.origin), str(self.server))
        self.url = "https://git.example.test/mobile/" + name + ".git"
        self.git(self.server, "remote", "set-url", "origin", self.url)
        # The production URL remains registry-bound. A trusted local Git rewrite
        # routes test transfers to a bare repository, avoiding network credentials.
        self.git(self.server, "config", "url.{}.insteadOf".format(self.origin.as_uri()), self.url)
        self.git(self.server, "config", "protocol.file.allow", "always")
        self.git(self.developer, "checkout", "-q", "-b", "feature/test")
        self.head = self.commit("base\nfeature\n")
        self.git(self.developer, "push", "-q", "origin", "feature/test")
        self.registry = project_registry(self.server, project=name, project_path="mobile/" + name)
        self.mr = GitLabMergeRequest(name, name, 42,
            "https://git.example.test/mobile/{}/merge_requests/42".format(name),
            "opened", "feature/test", "main", self.base, self.base, self.head)

    @staticmethod
    def git(path, *arguments):
        """Runs isolated fixture commands and retains only their small stdout."""

        return subprocess.run(["git", "-C", str(path), *arguments], check=True,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True).stdout.strip()

    def commit(self, text):
        """Commits one source change on the currently selected developer branch."""

        (self.developer / "source.txt").write_text(text, encoding="utf-8")
        self.git(self.developer, "add", "source.txt")
        self.git(self.developer, "commit", "-q", "-m", self.name + " fixture change")
        return self.git(self.developer, "rev-parse", "HEAD")

    def has_commit(self, sha):
        """Reports whether the server clone has a fixed commit object."""

        return subprocess.run(["git", "-C", str(self.server), "cat-file", "-e", sha + "^{commit}"],
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0


class GitRepositorySynchronizationTests(unittest.TestCase):
    """Verifies branch freshness, fixed identities, isolation and worktree preservation."""

    def setUp(self):
        """Creates a stale checkout and a separate developer's pushed MR."""

        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.workspace = GitSyncWorkspace(self.root)
        self.cancel = threading.Event()
        self.synchronizer = GitRepositorySynchronizer(self.workspace.registry, self.cancel)

    def tearDown(self):
        """Deletes only the fixture's temporary repositories."""

        self.temporary.cleanup()

    def test_fetches_pushed_source_and_preserves_dirty_worktree_and_normal_refs(self):
        """Fills missing objects without changing HEAD, index, local branches or origin refs."""

        workspace = self.workspace
        (workspace.server / "source.txt").write_text("local edit\n", encoding="utf-8")
        (workspace.server / "untracked.txt").write_text("local\n", encoding="utf-8")
        workspace.git(workspace.server, "config", "fetch.prune", "true")
        workspace.git(workspace.server, "config", "fetch.pruneTags", "true")
        workspace.git(workspace.server, "update-ref", "refs/remotes/origin/deleted", workspace.base)
        workspace.git(workspace.server, "tag", "local-only", workspace.base)
        before = {command: workspace.git(workspace.server, *command) for command in (
            ("rev-parse", "HEAD"), ("status", "--porcelain"),
            ("show-ref", "--heads"), ("for-each-ref", "refs/remotes"), ("show-ref", "--tags"),
            ("diff",), ("diff", "--cached"))}
        self.assertFalse(workspace.has_commit(workspace.head))
        self.synchronizer.synchronize(workspace.mr)
        self.assertTrue(workspace.has_commit(workspace.head))
        workspace.registry.repository("app-a").validate_build_snapshot(workspace.head, workspace.base, workspace.base)
        for command, value in before.items():
            self.assertEqual(workspace.git(workspace.server, *command), value)
        self.assertEqual(workspace.git(workspace.server, "rev-parse", "refs/ioscoverage/commits/" + workspace.head), workspace.head)

    def test_target_only_advance_is_fetched_even_with_identical_mr_diff_refs(self):
        """Does not use MR head equality to skip synchronization of the target branch."""

        workspace = self.workspace
        self.synchronizer.synchronize(workspace.mr)
        workspace.git(workspace.developer, "checkout", "-q", "main")
        target = workspace.commit("base\ntarget advance\n")
        workspace.git(workspace.developer, "push", "-q", "origin", "main")
        self.assertFalse(workspace.has_commit(target))
        self.synchronizer.synchronize(workspace.mr)
        self.assertTrue(workspace.has_commit(target))

    def test_force_push_preserves_previous_fixed_commit_and_fetches_new_head(self):
        """Retains the original evidence commit while accepting a rewritten source tip."""

        workspace = self.workspace
        self.synchronizer.synchronize(workspace.mr)
        workspace.git(workspace.developer, "reset", "--hard", workspace.base)
        rewritten = workspace.commit("base\nrewritten feature\n")
        workspace.git(workspace.developer, "push", "-q", "--force", "origin", "feature/test")
        self.synchronizer.synchronize(replace(workspace.mr, head_sha=rewritten))
        self.assertTrue(workspace.has_commit(rewritten))
        self.assertEqual(workspace.git(workspace.server, "rev-parse", "refs/ioscoverage/commits/" + workspace.head), workspace.head)

    def test_a_second_project_never_receives_first_project_objects(self):
        """Uses project-specific repositories without product-name or sibling-path inference."""

        other = GitSyncWorkspace(self.root, "app-b")
        GitRepositorySynchronizer(other.registry, self.cancel).synchronize(other.mr)
        self.assertTrue(other.has_commit(other.head))
        self.assertFalse(self.workspace.has_commit(other.head))
        self.assertFalse(other.has_commit(self.workspace.head))

    def test_mismatched_remote_and_unsafe_branch_are_rejected_before_fetch(self):
        """Rejects a remote for another project and invalid refspec-shaped branch names."""

        workspace = self.workspace
        workspace.git(workspace.server, "remote", "set-url", "origin", "https://git.example.test/other/project.git")
        with self.assertRaises(RepositorySyncError) as caught:
            self.synchronizer.synchronize(workspace.mr)
        self.assertEqual(caught.exception.code, "repository_sync_remote_mismatch")
        self.assertFalse(workspace.has_commit(workspace.head))
        for branch in ("--upload-pack=bad", "a:b", "main\nother", "../main"):
            with self.subTest(branch=branch), self.assertRaises(RepositorySyncError):
                self.synchronizer.synchronize(replace(workspace.mr, source_branch=branch))

    def test_origin_forms_are_bounded_to_the_registered_host_and_path(self):
        """Accepts common read transports and rejects URL credentials and project drift."""

        target = GitLabProject("git.example.test", "mobile/app-a")
        for url in ("https://git.example.test/mobile/app-a.git", "git@git.example.test:mobile/app-a.git",
                    "ssh://git@git.example.test/mobile/app-a.git"):
            self.assertTrue(self.synchronizer._matches_project(url, target), url)
        for url in ("https://user:secret@git.example.test/mobile/app-a.git", "file:///tmp/app-a",
                    "https://other.test/mobile/app-a.git", "https://git.example.test/mobile/app-b.git",
                    "ext::bad", "https://git.example.test/mobile/app-a.git?token=secret"):
            self.assertFalse(self.synchronizer._matches_project(url, target), url)

    def test_failed_fetch_is_redacted_and_cancellation_starts_no_command(self):
        """Discards private Git stderr and refuses work after service shutdown."""

        self.workspace.git(self.workspace.developer, "push", "-q", "origin", "--delete", "feature/test")
        with self.assertRaises(RepositorySyncError) as caught:
            self.synchronizer.synchronize(self.workspace.mr)
        self.assertNotIn(str(self.root), str(caught.exception))
        self.assertFalse(self.synchronizer.has_active_process_group())
        self.cancel.set()
        with patch("ioscoverage.integrations.git_sync.subprocess.Popen") as popen:
            with self.assertRaises(RepositorySyncError) as caught:
                self.synchronizer.synchronize(self.workspace.mr)
            self.assertEqual(caught.exception.code, "repository_sync_stopping")
            popen.assert_not_called()

    def test_command_timeout_stops_its_entire_process_group(self):
        """Prevents a timed-out Git transport child from outliving its command owner."""

        self.synchronizer.command_timeout_seconds = 0.1
        started = time.monotonic()
        with self.assertRaises(RepositorySyncError) as caught:
            self.synchronizer._run(self.workspace.server, ["-c", "alias.block=!exec sleep 30", "block"])
        self.assertEqual(caught.exception.code, "repository_sync_timeout")
        self.assertLess(time.monotonic() - started, 5)
        self.assertFalse(self.synchronizer.has_active_process_group())

    def test_configured_ssh_transport_and_options_are_preserved(self):
        """Uses selected commands and routing arguments through real credential-free fetches."""

        script = self.root / "configured ssh.py"
        marker = self.root / "ssh-arguments.json"
        script.write_text("#!" + sys.executable + "\n" +
            "import json, os, sys\n"
            "if '-G' in sys.argv: sys.exit(0)\n" +
            "open({!r}, 'w').write(json.dumps(sys.argv[1:]))\n".format(str(marker)) +
            "os.execvp('git-upload-pack', ['git-upload-pack', {!r}])\n".format(str(self.workspace.origin)), encoding="utf-8")
        script.chmod(0o755)
        fallback_dir = self.root / "bin"
        fallback_dir.mkdir()
        fallback = fallback_dir / "ssh"
        fallback.write_text("#!/bin/sh\nexit 99\n", encoding="utf-8")
        fallback.chmod(0o755)
        self.workspace.git(self.workspace.server, "remote", "set-url", "origin", "git@git.example.test:mobile/app-a.git")
        selections = (
            ({}, shlex.quote(str(script)) + " --identity fixture-key --proxy fixture-route", ("--identity", "fixture-key", "--proxy", "fixture-route")),
            ({"GIT_SSH_COMMAND": shlex.quote(str(script)) + " --identity env-key"}, "must-not-be-used", ("--identity", "env-key")),
            ({"GIT_SSH": str(script)}, None, ()),
        )
        for variables, configured, expected in selections:
            with self.subTest(selection=tuple(variables) or ("core.sshCommand",)):
                environment = dict(os.environ)
                for key in ("GIT_SSH_COMMAND", "GIT_SSH", "GIT_SSH_VARIANT"):
                    environment.pop(key, None)
                environment.update(variables)
                environment["PATH"] = str(fallback_dir) + os.pathsep + environment["PATH"]
                if configured is None:
                    self.workspace.git(self.workspace.server, "config", "--unset-all", "core.sshCommand")
                else:
                    self.workspace.git(self.workspace.server, "config", "core.sshCommand", configured)
                if marker.exists():
                    marker.unlink()
                with patch.dict(os.environ, environment, clear=True):
                    self.synchronizer.synchronize(self.workspace.mr)
                arguments = json.loads(marker.read_text(encoding="utf-8"))
                for argument in expected:
                    self.assertIn(argument, arguments)
                self.assertTrue(self.workspace.has_commit(self.workspace.head))

    def test_git_askpass_fallbacks_are_never_executed(self):
        """Suppresses environment/config askpass without contacting an HTTPS endpoint."""

        marker = self.root / "askpass-called"
        askpass = self.root / "askpass"
        askpass.write_text("#!/bin/sh\nprintf called >> " + shlex.quote(str(marker)) +
                           "\nprintf 'fixture-answer\\n'\n", encoding="utf-8")
        askpass.chmod(0o755)
        probe = "!printf 'protocol=https\\nhost=git.example.test\\n\\n' | git -c credential.helper= credential fill"
        for source in ("GIT_ASKPASS", "core.askPass", "SSH_ASKPASS"):
            with self.subTest(source=source):
                environment = dict(os.environ)
                for key in ("GIT_ASKPASS", "SSH_ASKPASS"):
                    environment.pop(key, None)
                environment.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull)
                self.workspace.git(self.workspace.server, "config", "core.askPass", str(askpass))
                if source != "core.askPass":
                    self.workspace.git(self.workspace.server, "config", "--unset-all", "core.askPass")
                    environment[source] = str(askpass)
                if marker.exists():
                    marker.unlink()
                with patch.dict(os.environ, environment, clear=True):
                    self.synchronizer._run(self.workspace.server, ["-c", "alias.credential-probe=" + probe,
                                                                   "credential-probe"], check=False)
                self.assertFalse(marker.exists(), source + " was invoked")

    def test_noninteractive_credential_helper_remains_available(self):
        """Reads a fixture credential from a helper without executing configured askpass."""

        helper = self.root / "credential-helper"
        helper.write_text("#!/bin/sh\nprintf 'username=fixture-user\\npassword=fixture-value\\n'\n", encoding="utf-8")
        helper.chmod(0o755)
        probe = ("!printf 'protocol=https\\nhost=git.example.test\\n\\n' | git -c credential.helper= "
                 "-c " + shlex.quote("credential.helper=" + str(helper)) + " credential fill")
        output = self.synchronizer._run(self.workspace.server, ["-c", "alias.credential-probe=" + probe,
                                                               "credential-probe"])
        self.assertIn("username=fixture-user", output)
        self.assertIn("password=fixture-value", output)

    def test_shutdown_cancellation_stops_a_running_git_process_group(self):
        """Releases a blocked command promptly when the service requests shutdown."""

        entered = threading.Event()
        errors = []
        original_popen = subprocess.Popen
        def start(*arguments, **keywords):
            """Signals when the real command has been launched into its own group."""

            process = original_popen(*arguments, **keywords)
            entered.set()
            return process
        def run():
            """Owns one slow fixture command and records its stable cancellation."""

            try:
                self.synchronizer._run(self.workspace.server, ["-c", "alias.block=!exec sleep 30", "block"])
            except RepositorySyncError as error:
                errors.append(error)
        with patch("ioscoverage.integrations.git_sync.subprocess.Popen", side_effect=start):
            worker = threading.Thread(target=run)
            worker.start()
            self.assertTrue(entered.wait(timeout=3))
            self.cancel.set()
            worker.join(timeout=5)
        self.assertFalse(worker.is_alive())
        self.assertEqual([error.code for error in errors], ["repository_sync_stopping"])
        self.assertFalse(self.synchronizer.has_active_process_group())
