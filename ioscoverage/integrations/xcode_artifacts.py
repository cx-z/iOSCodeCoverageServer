#!/usr/bin/env python3
"""Xcode 26.3 UUID validation for immutable iOS Build artifacts."""

import os
import re
import shutil
import stat
import subprocess
import tempfile
import uuid
import zipfile
from pathlib import Path, PurePosixPath
from typing import Callable, Dict, List, Mapping, Optional, Sequence, Set

from ioscoverage.lifecycle.runtime_lock import inherited_runtime_lock_descriptors


DEFAULT_DEVELOPER_DIR = Path("/Applications/Xcode263.app/Contents/Developer")
TOOL_TIMEOUT_SECONDS = 30
MAX_DSYM_ZIP_ENTRIES = 1024
UUID_LINE_PATTERN = re.compile(r"^UUID:\s+([0-9A-Fa-f-]{36})(?:\s|$)")
MACHO_MAGICS = {
    b"\xfe\xed\xfa\xce",
    b"\xce\xfa\xed\xfe",
    b"\xfe\xed\xfa\xcf",
    b"\xcf\xfa\xed\xfe",
    b"\xca\xfe\xba\xbe",
    b"\xbe\xba\xfe\xca",
    b"\xca\xfe\xba\xbf",
    b"\xbf\xba\xfe\xca",
}


class ArtifactValidationError(Exception):
    """Carries one stable artifact-validation error code and safe message."""

    def __init__(self, code: str, message: str):
        """Creates an error without coupling Xcode tooling to HTTP types."""

        super().__init__(message)
        self.code = code
        self.message = message


ArtifactCommandRunner = Callable[[Sequence[str], int], subprocess.CompletedProcess]


class XcodeArtifactValidator:
    """Validates Mach-O and dSYM UUIDs with one fixed Xcode 26.3 toolchain."""

    def __init__(
        self,
        developer_dir: Path = DEFAULT_DEVELOPER_DIR,
        command_runner: Optional[ArtifactCommandRunner] = None,
        reject_stderr: bool = False,
    ):
        """Verifies Xcode 26.3 and retains its immutable developer directory."""

        self.developer_dir = developer_dir.resolve()
        self.command_runner = command_runner
        self.reject_stderr = reject_stderr
        version = self._run(["xcodebuild", "-version"], startup=True).stdout.splitlines()
        if not version or version[0].strip() != "Xcode 26.3":
            raise ArtifactValidationError(
                "xcode_toolchain_invalid", "configured developer directory must contain Xcode 26.3"
            )
        self._run(["xcrun", "--find", "dwarfdump"], startup=True)

    def validate(
        self,
        artifact_paths: Mapping[str, Path],
        declared_uuids: Mapping[str, Sequence[str]],
        coverage_binary: str,
        temporary_parent: Path,
        maximum_dsym_uncompressed_bytes: int,
    ) -> Dict[str, List[str]]:
        """Extracts actual UUIDs and enforces manifest and dSYM relationships."""

        actual: Dict[str, List[str]] = {
            "main_executable": self._macho_uuids(
                artifact_paths["main_executable"], "main_executable", required=True
            ),
            "dsym": self._dsym_uuids(
                artifact_paths["dsym"],
                temporary_parent,
                maximum_dsym_uncompressed_bytes,
            ),
        }
        if "debug_dylib" in artifact_paths:
            actual["debug_dylib"] = self._macho_uuids(
                artifact_paths["debug_dylib"], "debug_dylib", required=True
            )

        for field_name, actual_values in actual.items():
            declared_values = declared_uuids.get(field_name)
            if declared_values is None or set(declared_values) != set(actual_values):
                raise ArtifactValidationError(
                    "artifact_uuid_mismatch",
                    field_name + " UUIDs do not match the uploaded artifact",
                )

        coverage_uuids = set(actual[coverage_binary])
        if not coverage_uuids.issubset(set(actual["dsym"])):
            raise ArtifactValidationError(
                "coverage_binary_dsym_uuid_mismatch",
                "coverage_binary UUIDs are not all present in the uploaded dSYM",
            )
        return actual

    def _dsym_uuids(
        self, archive_path: Path, temporary_parent: Path, maximum_uncompressed_bytes: int
    ) -> List[str]:
        """Safely extracts one bounded dSYM ZIP and returns all actual DWARF UUIDs."""

        extraction_root = Path(
            tempfile.mkdtemp(prefix=".dsym-", dir=str(temporary_parent.resolve()))
        )
        try:
            self._extract_safe_zip(archive_path, extraction_root, maximum_uncompressed_bytes)
            discovered: Set[str] = set()
            for candidate in extraction_root.rglob("*"):
                if candidate.is_file() and self._is_macho_candidate(candidate):
                    discovered.update(self._macho_uuids(candidate, "dsym", required=False))
            if not discovered:
                raise ArtifactValidationError(
                    "dsym_uuid_unreadable", "uploaded dSYM ZIP contains no UUID-bearing DWARF file"
                )
            return sorted(discovered)
        finally:
            shutil.rmtree(str(extraction_root), ignore_errors=True)

    def _is_macho_candidate(self, path: Path) -> bool:
        """Uses file content, not a ZIP-provided filename, to select dwarfdump candidates."""

        try:
            with path.open("rb") as source:
                return source.read(4) in MACHO_MAGICS
        except OSError:
            return False

    def _extract_safe_zip(
        self, archive_path: Path, destination_root: Path, maximum_uncompressed_bytes: int
    ) -> None:
        """Extracts regular ZIP members without trusting archive paths or file types."""

        try:
            archive = zipfile.ZipFile(str(archive_path), mode="r")
        except (OSError, zipfile.BadZipFile) as error:
            raise ArtifactValidationError("invalid_dsym_zip", "dSYM must be a valid ZIP") from error
        with archive:
            entries = archive.infolist()
            if not entries or len(entries) > MAX_DSYM_ZIP_ENTRIES:
                raise ArtifactValidationError(
                    "invalid_dsym_zip", "dSYM ZIP has an invalid number of entries"
                )
            total_size = 0
            destinations: Set[Path] = set()
            for entry in entries:
                destination = self._safe_zip_destination(destination_root, entry)
                if destination in destinations:
                    raise ArtifactValidationError(
                        "unsafe_dsym_zip", "dSYM ZIP contains a duplicate destination"
                    )
                destinations.add(destination)
                if entry.is_dir():
                    destination.mkdir(parents=True, exist_ok=True)
                    continue
                total_size += entry.file_size
                if total_size > maximum_uncompressed_bytes:
                    raise ArtifactValidationError(
                        "dsym_archive_too_large",
                        "uncompressed dSYM exceeds the configured Build artifact limit",
                    )
                destination.parent.mkdir(parents=True, exist_ok=True)
                try:
                    with archive.open(entry, mode="r") as source, destination.open("xb") as output:
                        shutil.copyfileobj(source, output, length=1024 * 1024)
                except (OSError, RuntimeError, zipfile.BadZipFile) as error:
                    raise ArtifactValidationError(
                        "invalid_dsym_zip", "dSYM ZIP could not be safely extracted"
                    ) from error
                if destination.stat().st_size != entry.file_size:
                    raise ArtifactValidationError(
                        "invalid_dsym_zip", "dSYM ZIP entry size changed during extraction"
                    )

    def _safe_zip_destination(self, root: Path, entry: zipfile.ZipInfo) -> Path:
        """Maps one ZIP member to a confined regular destination path."""

        name = entry.filename
        path = PurePosixPath(name)
        mode = entry.external_attr >> 16
        file_type = stat.S_IFMT(mode)
        if (
            not name
            or "\\" in name
            or path.is_absolute()
            or ".." in path.parts
            or entry.flag_bits & 0x1
            or stat.S_ISLNK(mode)
            or file_type not in (0, stat.S_IFREG, stat.S_IFDIR)
        ):
            raise ArtifactValidationError(
                "unsafe_dsym_zip", "dSYM ZIP contains an unsafe entry"
            )
        destination = root.joinpath(*path.parts).resolve()
        try:
            destination.relative_to(root.resolve())
        except ValueError as error:
            raise ArtifactValidationError(
                "unsafe_dsym_zip", "dSYM ZIP entry escapes the extraction directory"
            ) from error
        return destination

    def _macho_uuids(self, path: Path, field_name: str, required: bool) -> List[str]:
        """Uses Xcode dwarfdump to extract normalized UUIDs from one candidate file."""

        result = self._run(["xcrun", "dwarfdump", "--uuid", str(path)], check=False)
        values: Set[str] = set()
        if result.returncode == 0:
            for line in result.stdout.splitlines():
                match = UUID_LINE_PATTERN.match(line.strip())
                if match:
                    try:
                        values.add(str(uuid.UUID(match.group(1))))
                    except ValueError:
                        continue
        if required and not values:
            raise ArtifactValidationError(
                "artifact_uuid_unreadable", field_name + " is not a UUID-bearing Mach-O"
            )
        return sorted(values)

    def _run(
        self, arguments: Sequence[str], check: bool = True, startup: bool = False
    ) -> subprocess.CompletedProcess:
        """Runs one fixed Xcode command with the required developer directory."""

        environment = dict(os.environ)
        environment["DEVELOPER_DIR"] = str(self.developer_dir)
        inherited_descriptors = inherited_runtime_lock_descriptors()
        try:
            if self.command_runner is not None:
                result = self.command_runner(arguments, TOOL_TIMEOUT_SECONDS)
            else:
                result = subprocess.run(
                    list(arguments),
                    check=False,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    timeout=TOOL_TIMEOUT_SECONDS,
                    env=environment,
                    pass_fds=inherited_descriptors,
                )
        except (OSError, subprocess.TimeoutExpired) as error:
            code = "xcode_toolchain_invalid" if startup else "artifact_tool_unavailable"
            raise ArtifactValidationError(code, "Xcode artifact tool could not be executed") from error
        if check and result.returncode != 0:
            code = "xcode_toolchain_invalid" if startup else "artifact_tool_unavailable"
            raise ArtifactValidationError(code, "Xcode artifact tool could not be executed")
        if self.reject_stderr and result.stderr:
            raise ArtifactValidationError(
                "artifact_tool_warning", "Xcode artifact tool emitted stderr"
            )
        return result
