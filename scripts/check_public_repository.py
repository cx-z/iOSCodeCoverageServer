#!/usr/bin/env python3
"""Reject private deployment details and credentials from Git publication candidates."""

import argparse
import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, List, Sequence, Tuple


def _joined_bytes(*parts: str) -> bytes:
    """Builds a case-insensitive byte needle without publishing the private term itself."""

    return "".join(parts).casefold().encode("utf-8")


FORBIDDEN_TERMS: Tuple[Tuple[str, bytes], ...] = (
    ("legacy_product_name", _joined_bytes("k", "k")),
    ("legacy_product_name", _joined_bytes("knock", "nock")),
    ("legacy_repository_name", _joined_bytes("k", "kcodecoverage")),
    ("internal_organization_name", _joined_bytes("pp", "chat")),
    ("internal_organization_name", _joined_bytes("pixel", "muse")),
    ("internal_product_name", _joined_bytes("do", "kimo")),
    ("internal_organization_name", _joined_bytes("xiao", "chuan")),
    ("internal_organization_name", _joined_bytes("xun", "lei")),
    ("internal_gitlab_group", _joined_bytes("agi", "-chat")),
    ("private_home_path", _joined_bytes("/Users/", "zc", "x")),
    ("internal_organization_name", _joined_bytes("小", "川")),
    ("internal_organization_name", _joined_bytes("迅", "雷")),
)

SENSITIVE_FILENAMES = frozenset(
    {
        "".join(("operator", "_token")),
        "".join(("run_upload", "_token")),
        "".join(("gitlab_api", "_token")),
    }
)

SECRET_PATTERNS: Tuple[Tuple[str, re.Pattern[bytes]], ...] = (
    ("gitlab_access_token", re.compile(b"gl" + b"pat-[A-Za-z0-9_-]{16,}")),
    ("github_access_token", re.compile(b"gh[pousr]_[A-Za-z0-9]{20,}")),
    ("github_access_token", re.compile(b"github_pat_[A-Za-z0-9_]{20,}")),
    (
        "private_key_material",
        re.compile(b"-----BEGIN [A-Z0-9 ]*PRIVATE" + b" KEY-----"),
    ),
    ("local_home_path", re.compile(br"/(?:Users|home)/[A-Za-z0-9._-]+/")),
)

RUNTIME_SUFFIXES = frozenset({".db", ".sqlite", ".sqlite3", ".profraw", ".profdata", ".log"})


class RepositoryScanError(RuntimeError):
    """Reports a safe repository-enumeration failure."""


@dataclass(frozen=True, order=True)
class Finding:
    """Identifies one publication risk without retaining or displaying matched bytes."""

    path: str
    kind: str


def publication_candidates(repository: Path) -> Tuple[Path, ...]:
    """Returns tracked and non-ignored untracked files, including a pre-commit tree."""

    root = repository.resolve()
    completed = subprocess.run(
        ["git", "-C", str(root), "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if completed.returncode != 0:
        raise RepositoryScanError("unable to enumerate Git publication candidates")
    if completed.stderr:
        raise RepositoryScanError("Git candidate enumeration produced diagnostics")
    candidates = []
    for raw_path in completed.stdout.split(b"\0"):
        if not raw_path:
            continue
        try:
            relative = Path(os.fsdecode(raw_path))
        except UnicodeError as error:
            raise RepositoryScanError("a Git candidate path is not decodable") from error
        candidate = root / relative
        if candidate.exists() or candidate.is_symlink():
            candidates.append(candidate)
    return tuple(candidates)


def scan_repository(repository: Path) -> Tuple[Finding, ...]:
    """Scans candidate paths and bytes while returning only safe finding metadata."""

    root = repository.resolve()
    findings: List[Finding] = []
    for candidate in publication_candidates(root):
        relative = candidate.relative_to(root).as_posix()
        path_bytes = relative.casefold().encode("utf-8", errors="replace")
        if candidate.name in SENSITIVE_FILENAMES:
            findings.append(Finding(relative, "credential_file"))
        if candidate.suffix.casefold() in RUNTIME_SUFFIXES:
            findings.append(Finding(relative, "runtime_artifact"))
        content = _read_candidate_bytes(candidate)
        findings.extend(_scan_bytes(relative, path_bytes, FORBIDDEN_TERMS))
        findings.extend(_scan_bytes(relative, content.lower(), FORBIDDEN_TERMS))
        for kind, pattern in SECRET_PATTERNS:
            if pattern.search(path_bytes) or pattern.search(content):
                findings.append(Finding(relative, kind))
    return tuple(sorted(set(findings)))


def _read_candidate_bytes(path: Path) -> bytes:
    """Reads file bytes without following a publication-candidate symbolic link."""

    try:
        if path.is_symlink():
            return os.fsencode(os.readlink(path))
        return path.read_bytes()
    except OSError as error:
        raise RepositoryScanError("unable to inspect a Git publication candidate") from error


def _scan_bytes(
    relative: str, content: bytes, rules: Iterable[Tuple[str, bytes]]
) -> List[Finding]:
    """Returns type-only findings for fixed byte needles."""

    return [Finding(relative, kind) for kind, needle in rules if needle in content]


def build_parser() -> argparse.ArgumentParser:
    """Builds the bounded command-line interface."""

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--repository",
        type=Path,
        default=Path(__file__).resolve().parents[1],
        help="Git repository to inspect (defaults to this source repository)",
    )
    return parser


def main(arguments: Sequence[str] = None) -> int:
    """Prints only relative paths and safe categories, then returns a CI-friendly code."""

    parsed = build_parser().parse_args(arguments)
    try:
        findings = scan_repository(parsed.repository)
    except RepositoryScanError as error:
        print("public repository check failed: " + str(error), file=sys.stderr)
        return 2
    if findings:
        for finding in findings:
            print("{}: {}".format(json.dumps(finding.path, ensure_ascii=True), finding.kind))
        print("public repository check failed: {} finding(s)".format(len(findings)), file=sys.stderr)
        return 1
    print("public repository check passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
