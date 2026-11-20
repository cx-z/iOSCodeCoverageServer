"""Bounded Git object synchronization for registry-selected repositories."""

import hashlib
import os
import re
import signal
import subprocess
import time
from urllib.parse import urlsplit

from ioscoverage.integrations.git_repository import GIT_SHA_PATTERN
from ioscoverage.lifecycle.runtime_lock import inherited_runtime_lock_descriptors


class RepositorySyncError(Exception):
    """Carries a stable error without remote URLs, credentials, or command output."""

    def __init__(self, code, message):
        """Retains only safe diagnostic fields."""

        super().__init__(message)
        self.code, self.message = code, message


class GitRepositorySynchronizer:
    """Fetches fixed MR inputs; one service worker exclusively owns this instance."""

    def __init__(self, registry, cancellation_event, runtime_lock_descriptor=None,
                 command_timeout_seconds=30.0):
        """Captures startup-selected projects and the server's cancellation boundary."""

        self.registry = registry
        self.cancellation_event = cancellation_event
        self.runtime_lock_descriptor = runtime_lock_descriptor
        self.command_timeout_seconds = command_timeout_seconds
        self._active = None

    def synchronize(self, merge_request, required_shas=()):
        """Fetches both branch histories, verifies exact commits, and retains their refs."""

        definition = self.registry.require(merge_request.project)
        repository = self.registry.repository(definition.project)
        shas = tuple(sorted(set((merge_request.base_sha, merge_request.start_sha,
                                 merge_request.head_sha, *required_shas))))
        if any(not isinstance(sha, str) or not GIT_SHA_PATTERN.fullmatch(sha) for sha in shas):
            raise RepositorySyncError("repository_sync_invalid_sha", "required commit identity is invalid")
        branches = sorted(set((merge_request.source_branch, merge_request.target_branch)))
        for branch in branches:
            if (not isinstance(branch, str) or not branch or branch.startswith("-")
                    or any(char in branch for char in ("\0", "\n", "\r"))):
                raise RepositorySyncError("repository_sync_invalid_branch", "MR branch identity is invalid")
            self._run(repository.path, ["check-ref-format", "refs/heads/" + branch])
        urls = self._run(repository.path, ["config", "--get-all", "remote.origin.url"]).splitlines()
        if len(urls) != 1 or not self._matches_project(urls[0], definition.gitlab):
            raise RepositorySyncError(
                "repository_sync_remote_mismatch", "repository origin must match its registered GitLab project")

        # Private refs never move a developer branch, index, worktree or normal remote refs.
        refspecs = ["+refs/heads/{}:refs/ioscoverage/branches/{}".format(
            branch, hashlib.sha256(branch.encode("utf-8")).hexdigest()) for branch in branches]
        self._fetch(repository.path, refspecs)
        for sha in shas:
            if not self._has_commit(repository.path, sha):
                self._fetch(repository.path, [sha + ":refs/ioscoverage/commits/" + sha])
            if not self._has_commit(repository.path, sha):
                raise RepositorySyncError("repository_sync_object_missing", "required commit is unavailable after synchronization")
            # Keep immutable inputs reachable even after subsequent force pushes.
            self._run(repository.path, ["update-ref", "refs/ioscoverage/commits/" + sha, sha])

    @staticmethod
    def _matches_project(url, gitlab):
        """Accepts ordinary HTTPS/SSH origin forms for exactly the registered project."""

        if any(char.isspace() for char in url):
            return False
        if "://" in url:
            try:
                parsed = urlsplit(url)
                port = parsed.port
            except ValueError:
                return False
            if (parsed.scheme not in ("https", "ssh") or parsed.password is not None
                    or parsed.query or parsed.fragment
                    or (parsed.scheme == "https" and parsed.username is not None)
                    or (port is not None and not 1 <= port <= 65535)):
                return False
            host, path = parsed.hostname, parsed.path.removeprefix("/")
        else:
            match = re.fullmatch(r"(?:[^/@:]+@)?([^/:]+):([^:]+)", url)
            if match is None:
                return False
            host, path = match.groups()
        return host == gitlab.host and path.removesuffix(".git") == gitlab.project_path

    def _fetch(self, path, refspecs):
        """Fetches explicit refs without pruning, tags, submodules, or automatic GC."""

        self._run(path, ["-c", "http.followRedirects=false", "fetch", "--quiet", "--atomic",
                         "--refmap=", "--no-prune", "--no-prune-tags", "--no-tags",
                         "--no-write-fetch-head", "--no-auto-maintenance", "--no-write-commit-graph",
                         "--recurse-submodules=no", "origin", *refspecs])

    def _has_commit(self, path, sha):
        """Checks an exact object without lazy network fetching."""

        return self._run(path, ["cat-file", "-e", sha + "^{commit}"], check=False) is not None

    def _run(self, path, arguments, check=True):
        """Runs a noninteractive argv in a cancellable process group, discarding stderr."""

        if self.has_active_process_group():
            raise RepositorySyncError("repository_sync_termination_failed", "previous Git synchronization has not stopped")
        if self.cancellation_event.is_set():
            raise RepositorySyncError("repository_sync_stopping", "repository synchronization is stopping")
        environment = dict(os.environ)
        # Keep Git's configured SSH command/identity/proxy selection. A new
        # session and DEVNULL stdin provide no terminal; deny askpass fallbacks
        # explicitly while leaving noninteractive credential helpers available.
        environment.update(GIT_TERMINAL_PROMPT="0", GCM_INTERACTIVE="never", GIT_NO_LAZY_FETCH="1",
                           GIT_ASKPASS="/usr/bin/false", SSH_ASKPASS="/usr/bin/false",
                           SSH_ASKPASS_REQUIRE="never")
        inherited_descriptors = ((self.runtime_lock_descriptor,)
                                 if self.runtime_lock_descriptor is not None
                                 else inherited_runtime_lock_descriptors())
        try:
            process = subprocess.Popen(
                ["git", "-C", str(path), *arguments], stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
                encoding="utf-8", errors="replace", env=environment,
                start_new_session=True, pass_fds=inherited_descriptors,
            )
        except OSError:
            raise RepositorySyncError("repository_sync_failed", "Git synchronization could not be started") from None
        self._active = process
        deadline = time.monotonic() + self.command_timeout_seconds
        try:
            while True:
                if self.cancellation_event.is_set():
                    raise RepositorySyncError("repository_sync_stopping", "repository synchronization is stopping")
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise RepositorySyncError("repository_sync_timeout", "Git synchronization timed out")
                try:
                    stdout, _ = process.communicate(timeout=min(0.1, remaining))
                    break
                except subprocess.TimeoutExpired:
                    continue
        finally:
            try:
                self._stop_process_group(process)
            finally:
                process.stdout.close()
        if process.returncode != 0:
            if check:
                raise RepositorySyncError("repository_sync_failed", "Git synchronization failed; check repository read access and refs")
            return None
        return stdout

    def has_active_process_group(self):
        """Checks worker-owned process state only on the worker or after it is joined."""

        if self._active is None:
            return False
        if self._group_exists(self._active.pid):
            return True
        self._active = None
        return False

    def _stop_process_group(self, process):
        """Reaps the leader and proves child absence before permitting another command."""

        for signal_number in (signal.SIGTERM, signal.SIGKILL):
            if not self._group_exists(process.pid):
                self._active = None
                return
            try:
                os.killpg(process.pid, signal_number)
            except ProcessLookupError:
                pass
            except PermissionError:
                # Continue probing; a denied signal cannot prove the group exited.
                pass
            deadline = time.monotonic() + 2.0
            while time.monotonic() < deadline:
                process.poll()
                if not self._group_exists(process.pid):
                    self._active = None
                    return
                time.sleep(0.01)
        raise RepositorySyncError("repository_sync_termination_failed", "Git synchronization process group could not be stopped safely")

    @staticmethod
    def _group_exists(group_id):
        """Treats a denied probe as active rather than releasing execution ownership."""

        try:
            os.killpg(group_id, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        return True
