#!/usr/bin/env python3
"""Read-only Git object validation for immutable iOS Build snapshots."""

import os
import re
import subprocess
from pathlib import Path, PurePosixPath
from typing import List

from ioscoverage.lifecycle.runtime_lock import inherited_runtime_lock_descriptors


GIT_SHA_PATTERN = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
GIT_COMMAND_TIMEOUT_SECONDS = 10


class GitRepositoryError(Exception):
    """Carries a stable Git validation error without HTTP coupling."""

    def __init__(self, code: str, message: str):
        """Creates one safe repository validation error."""

        super().__init__(message)
        self.code = code
        self.message = message


class GitRepository:
    """Runs a fixed set of read-only commands against one startup-configured repository."""

    def __init__(self, path: Path):
        """Validates and retains one configured project worktree root."""

        self.path = path.resolve()
        if not self.path.is_dir():
            raise GitRepositoryError(
                "git_repository_invalid", "configured project repository is not a directory"
            )
        top_level = self._run(["rev-parse", "--show-toplevel"], startup=True).stdout.strip()
        if not top_level or Path(top_level).resolve() != self.path:
            raise GitRepositoryError(
                "git_repository_invalid",
                "configured project repository must be the Git worktree root",
            )

    def validate_build_snapshot(
        self, git_sha: str, target_sha_at_build: str, merge_base_sha: str
    ) -> None:
        """Confirms all commits exist and the declared merge-base is exact."""

        fields = (
            ("git_sha", git_sha),
            ("target_sha_at_build", target_sha_at_build),
            ("merge_base_sha", merge_base_sha),
        )
        for field_name, sha in fields:
            if not GIT_SHA_PATTERN.fullmatch(sha):
                raise GitRepositoryError(
                    "invalid_git_sha", field_name + " must be a full Git SHA"
                )
            result = self._run(["cat-file", "-e", sha + "^{commit}"], check=False)
            if result.returncode != 0:
                raise GitRepositoryError(
                    "git_object_not_found",
                    field_name + " is not a commit in the configured project repository",
                )

        result = self._run(["merge-base", target_sha_at_build, git_sha], check=False)
        actual_merge_base = result.stdout.strip().lower()
        if result.returncode != 0 or not GIT_SHA_PATTERN.fullmatch(actual_merge_base):
            raise GitRepositoryError(
                "merge_base_unavailable",
                "git merge-base could not resolve the Build comparison snapshot",
            )
        if actual_merge_base != merge_base_sha:
            raise GitRepositoryError(
                "merge_base_mismatch",
                "merge_base_sha does not match git merge-base target_sha_at_build git_sha",
            )

    def source(self, sha: str, path: str) -> str:
        """Reads a validated fixed blob, never a worktree file or revision expression."""

        if not GIT_SHA_PATTERN.fullmatch(sha):
            raise GitRepositoryError("invalid_git_sha", "commit must be a full SHA")
        self._validate_source_path(path)
        return self._run(["show", sha + ":" + path]).stdout

    @staticmethod
    def _validate_source_path(path):
        """Rejects unsafe blob names before fixed source or literal-path diff operations."""

        if (not isinstance(path, str) or not path or PurePosixPath(path).is_absolute() or
                path != PurePosixPath(path).as_posix() or ".." in PurePosixPath(path).parts or
                any(char in path for char in ("\0", "\n", "\r", "\t", "\\"))):
            raise GitRepositoryError("invalid_git_path", "source path must be repository-relative")

    def rename_status(self, old_sha, new_sha, old_path, new_path):
        """Asks Git about one competing rename pair, never a user-selected pathspec."""

        if any(not GIT_SHA_PATTERN.fullmatch(sha) for sha in (old_sha, new_sha)):
            raise GitRepositoryError("invalid_git_sha", "commits must be full SHAs")
        for path in (old_path, new_path):
            self._validate_source_path(path)
        return self._run(["--literal-pathspecs", "diff", "--no-ext-diff", "--no-textconv", "--find-renames",
                          "--name-status", "-z", old_sha, new_sha, "--", old_path, new_path]).stdout

    def diff_text(self, old_sha: str, new_sha: str):
        """Returns rename-aware fixed-object hunks with deterministic Git options."""

        if any(not GIT_SHA_PATTERN.fullmatch(sha) for sha in (old_sha, new_sha)):
            raise GitRepositoryError("invalid_git_sha", "commits must be full SHAs")
        common = ["-c", "core.quotePath=false", "diff", "--no-ext-diff", "--no-textconv",
                  "--find-renames", "--no-indent-heuristic", "--diff-algorithm=myers"]
        status = self._run(common + ["--name-status", "-z", old_sha, new_sha, "--"]).stdout
        patch = self._run(common + ["--unified=0", "--no-color", old_sha, new_sha, "--"]).stdout
        return status, patch

    def _run(
        self, arguments: List[str], check: bool = True, startup: bool = False
    ) -> subprocess.CompletedProcess:
        """Executes one predefined read-only Git operation without a shell."""

        environment = dict(os.environ)
        environment["GIT_OPTIONAL_LOCKS"] = "0"
        inherited_descriptors = inherited_runtime_lock_descriptors()
        try:
            result = subprocess.run(
                ["git", "-C", str(self.path)] + arguments,
                check=False,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=GIT_COMMAND_TIMEOUT_SECONDS,
                env=environment,
                pass_fds=inherited_descriptors,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            code = "git_repository_invalid" if startup else "git_repository_unavailable"
            raise GitRepositoryError(
                code, "configured project repository could not be inspected"
            ) from error
        if check and result.returncode != 0:
            code = "git_repository_invalid" if startup else "git_repository_unavailable"
            raise GitRepositoryError(code, "configured project repository could not be inspected")
        return result
