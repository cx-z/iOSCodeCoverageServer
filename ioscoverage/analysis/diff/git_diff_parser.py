#!/usr/bin/env python3
"""Strict parser for one fixed Git commit-to-commit new-side diff."""

import re
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Dict, List, Optional, Sequence, Tuple


HUNK_PATTERN = re.compile(
    r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@(?: .*)?$"
)
SUPPORTED_STATUS_CODES = {"A", "M", "D", "R", "T"}


class GitDiffParseError(Exception):
    """Describes an ambiguous or unsafe Git diff without guessing."""

    def __init__(self, code: str, message: str):
        """Creates one stable parser error."""

        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class GitFileStatus:
    """Represents one rename-aware Git file identity."""

    status: str
    old_path: Optional[str]
    new_path: Optional[str]
    similarity: Optional[int]


@dataclass(frozen=True)
class DiffHunk:
    """Stores one exact zero-context old/new range for line mapping."""

    old_start: int
    old_count: int
    new_start: int
    new_count: int


@dataclass(frozen=True)
class NormalizedDiffFile:
    """Contains only auditable new-side changed lines for one file."""

    status: str
    old_path: Optional[str]
    new_path: Optional[str]
    similarity: Optional[int]
    changed_new_lines: Tuple[int, ...]
    deleted_excluded: int
    binary: bool
    hunks: Tuple[DiffHunk, ...] = ()


def validate_repo_relative_path(value: str) -> str:
    """Accepts only normalized POSIX paths that cannot escape the repository."""

    if not isinstance(value, str) or not value or "\x00" in value:
        raise GitDiffParseError("unsafe_git_path", "Git path is empty or contains NUL")
    if any(character in value for character in ("\n", "\r", "\t")):
        raise GitDiffParseError(
            "unsupported_git_path", "Git paths containing control characters are unsupported"
        )
    path = PurePosixPath(value)
    if path.is_absolute() or value.startswith("/"):
        raise GitDiffParseError("unsafe_git_path", "absolute Git paths are forbidden")
    if value != path.as_posix() or any(part in ("", ".", "..") for part in path.parts):
        raise GitDiffParseError("unsafe_git_path", "Git path is not a normalized repository path")
    return value


def parse_name_status_z(payload: str) -> List[GitFileStatus]:
    """Parses `git diff --name-status -z` without whitespace path splitting."""

    if not isinstance(payload, str) or (payload and not payload.endswith("\x00")):
        raise GitDiffParseError("git_status_invalid", "NUL-delimited Git status is truncated")
    fields = payload.split("\x00")
    if fields and fields[-1] == "":
        fields.pop()
    result: List[GitFileStatus] = []
    index = 0
    while index < len(fields):
        token = fields[index]
        index += 1
        if not token:
            raise GitDiffParseError("git_status_invalid", "Git status token is empty")
        status = token[0]
        if status not in SUPPORTED_STATUS_CODES:
            raise GitDiffParseError("git_status_unsupported", "unsupported Git status: " + token)
        similarity: Optional[int] = None
        if status == "R":
            if not token[1:].isdigit() or not 0 <= int(token[1:]) <= 100:
                raise GitDiffParseError("git_status_invalid", "rename similarity is invalid")
            similarity = int(token[1:])
            if index + 1 >= len(fields):
                raise GitDiffParseError("git_status_invalid", "rename paths are truncated")
            old_path = validate_repo_relative_path(fields[index])
            new_path = validate_repo_relative_path(fields[index + 1])
            index += 2
        else:
            if token != status or index >= len(fields):
                raise GitDiffParseError("git_status_invalid", "Git status fields are malformed")
            path = validate_repo_relative_path(fields[index])
            index += 1
            old_path = None if status == "A" else path
            new_path = None if status == "D" else path
        result.append(GitFileStatus(status, old_path, new_path, similarity))
    return result


def parse_unified_zero_patch(
    patch: str, statuses: Sequence[GitFileStatus]
) -> List[NormalizedDiffFile]:
    """Parses hunk bodies in status order and keeps only added new-side line numbers."""

    sections = _split_patch_sections(patch)
    if len(sections) != len(statuses):
        raise GitDiffParseError(
            "git_diff_ambiguous", "Git patch sections do not match the file status list"
        )
    normalized: List[NormalizedDiffFile] = []
    for status, section in zip(statuses, sections):
        changed_lines: List[int] = []
        deleted_excluded = 0
        binary = False
        current_old: Optional[int] = None
        current_new: Optional[int] = None
        expected_old = 0
        expected_new = 0
        seen_old = 0
        seen_new = 0
        in_hunk = False
        hunks: List[DiffHunk] = []
        for line in section:
            match = HUNK_PATTERN.match(line)
            if match:
                if in_hunk and (seen_old != expected_old or seen_new != expected_new):
                    raise GitDiffParseError("git_diff_ambiguous", "Git hunk length is inconsistent")
                current_old = int(match.group(1))
                current_new = int(match.group(3))
                expected_old = int(match.group(2) or "1")
                expected_new = int(match.group(4) or "1")
                hunks.append(
                    DiffHunk(current_old, expected_old, current_new, expected_new)
                )
                seen_old = 0
                seen_new = 0
                in_hunk = True
                continue
            if line.startswith("Binary files ") or line == "GIT binary patch":
                binary = True
                in_hunk = False
                continue
            if not in_hunk:
                continue
            if line.startswith("+") and not line.startswith("+++"):
                if current_new is None:
                    raise GitDiffParseError("git_diff_ambiguous", "new-side hunk cursor is missing")
                changed_lines.append(current_new)
                current_new += 1
                seen_new += 1
            elif line.startswith("-") and not line.startswith("---"):
                if current_old is None:
                    raise GitDiffParseError("git_diff_ambiguous", "old-side hunk cursor is missing")
                current_old += 1
                seen_old += 1
                deleted_excluded += 1
            elif line.startswith(" "):
                if current_old is None or current_new is None:
                    raise GitDiffParseError("git_diff_ambiguous", "context hunk cursor is missing")
                current_old += 1
                current_new += 1
                seen_old += 1
                seen_new += 1
            elif line.startswith("\\ No newline at end of file"):
                continue
            else:
                raise GitDiffParseError("git_diff_ambiguous", "unsupported line inside Git hunk")
        if in_hunk and (seen_old != expected_old or seen_new != expected_new):
            raise GitDiffParseError("git_diff_ambiguous", "Git hunk length is inconsistent")
        if binary and changed_lines:
            raise GitDiffParseError("git_diff_ambiguous", "binary patch unexpectedly has text lines")
        normalized.append(
            NormalizedDiffFile(
                status=status.status,
                old_path=status.old_path,
                new_path=status.new_path,
                similarity=status.similarity,
                changed_new_lines=tuple(sorted(set(changed_lines))),
                deleted_excluded=deleted_excluded,
                binary=binary,
                hunks=tuple(hunks),
            )
        )
    return normalized


def normalized_diff_document(files: Sequence[NormalizedDiffFile]) -> Dict[str, object]:
    """Creates stable semantic JSON independent of patch formatting details."""

    return {
        "schema_version": 1,
        "files": [
            {
                "git_status": item.status,
                "old_path": item.old_path,
                "new_path": item.new_path,
                "similarity": item.similarity,
                "binary": item.binary,
                "changed_new_lines": list(item.changed_new_lines),
                "deleted_excluded": item.deleted_excluded,
            }
            for item in files
        ],
    }


def _split_patch_sections(patch: str) -> List[List[str]]:
    """Splits a patch on structural diff headers while preserving spaces in paths."""

    sections: List[List[str]] = []
    current: Optional[List[str]] = None
    for line in patch.splitlines():
        if line.startswith("diff --git "):
            current = [line]
            sections.append(current)
        elif current is not None:
            current.append(line)
        elif line:
            raise GitDiffParseError("git_diff_ambiguous", "Git patch has data before first file")
    return sections
