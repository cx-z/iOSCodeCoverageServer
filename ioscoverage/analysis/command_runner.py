#!/usr/bin/env python3
"""Audited subprocess execution for the local single-Build analyzer."""

import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Sequence

from ioscoverage.lifecycle.runtime_lock import inherited_runtime_lock_descriptors


DEFAULT_COMMAND_TIMEOUT_SECONDS = 300


class EvidenceCommandRunner:
    """Runs fixed argument arrays and records immutable command evidence."""

    def __init__(self, evidence_dir: Path, developer_dir: Path):
        """Confines all command output to one unpublished staging directory."""

        self.evidence_dir = evidence_dir.resolve()
        self.developer_dir = developer_dir.resolve()
        self.records: List[Dict[str, object]] = []
        self._automatic_index = 0

    def run(
        self,
        label: str,
        arguments: Sequence[str],
        stdout_filename: Optional[str] = None,
        timeout_seconds: int = DEFAULT_COMMAND_TIMEOUT_SECONDS,
    ) -> subprocess.CompletedProcess:
        """Executes one command without a shell and fsyncs stdout/stderr evidence."""

        if not label or any(character not in "abcdefghijklmnopqrstuvwxyz0123456789-" for character in label):
            raise ValueError("command label must contain lowercase letters, digits, and hyphens")
        stdout_name = stdout_filename or label + ".stdout.txt"
        stderr_name = label + ".stderr.txt"
        stdout_path = self.evidence_dir / stdout_name
        stderr_path = self.evidence_dir / stderr_name
        if stdout_path.exists() or stderr_path.exists():
            raise ValueError("command evidence filename already exists")

        environment = dict(os.environ)
        environment["DEVELOPER_DIR"] = str(self.developer_dir)
        inherited_descriptors = inherited_runtime_lock_descriptors()
        started_at = self._utc_now()
        stdout_text = ""
        stderr_text = ""
        timed_out = False
        exit_code = 127
        try:
            if stdout_filename is None:
                result = subprocess.run(
                    list(arguments),
                    check=False,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    timeout=timeout_seconds,
                    env=environment,
                    pass_fds=inherited_descriptors,
                )
                stdout_text = result.stdout
                stderr_text = result.stderr
                exit_code = result.returncode
                self._write_text(stdout_path, stdout_text)
            else:
                with stdout_path.open("xb") as stdout_file:
                    result = subprocess.run(
                        list(arguments),
                        check=False,
                        stdin=subprocess.DEVNULL,
                        stdout=stdout_file,
                        stderr=subprocess.PIPE,
                        text=True,
                        timeout=timeout_seconds,
                        env=environment,
                        pass_fds=inherited_descriptors,
                    )
                    stdout_file.flush()
                    os.fsync(stdout_file.fileno())
                stderr_text = result.stderr
                exit_code = result.returncode
        except subprocess.TimeoutExpired as error:
            timed_out = True
            exit_code = 124
            stderr_text = "command timed out after " + str(timeout_seconds) + " seconds\n"
            if error.stderr:
                stderr_text += self._as_text(error.stderr)
            if stdout_filename is None:
                stdout_text = self._as_text(error.stdout)
                self._write_text(stdout_path, stdout_text)
        except OSError as error:
            stderr_text = "command could not be executed: " + str(error) + "\n"
            if stdout_filename is None:
                self._write_text(stdout_path, "")
        self._write_text(stderr_path, stderr_text)
        completed_at = self._utc_now()
        record: Dict[str, object] = {
            "label": label,
            "arguments": list(arguments),
            "environment": {"DEVELOPER_DIR": str(self.developer_dir)},
            "started_at": started_at,
            "completed_at": completed_at,
            "exit_code": exit_code,
            "timed_out": timed_out,
            "stdout": stdout_name,
            "stderr": stderr_name,
            "stdout_size": stdout_path.stat().st_size if stdout_path.is_file() else 0,
            "stderr_size": stderr_path.stat().st_size,
        }
        self.records.append(record)
        return subprocess.CompletedProcess(
            list(arguments), exit_code, stdout=stdout_text, stderr=stderr_text
        )

    def run_for_artifact_validator(
        self, arguments: Sequence[str], timeout_seconds: int
    ) -> subprocess.CompletedProcess:
        """Adapts artifact-validator commands to the same evidence recorder."""

        self._automatic_index += 1
        executable = Path(arguments[0]).name.lower().replace("_", "-")
        label = "artifact-tool-{:02d}-{}".format(self._automatic_index, executable)
        return self.run(label, arguments, timeout_seconds=timeout_seconds)

    def _write_text(self, destination: Path, text: str) -> None:
        """Writes and fsyncs one UTF-8 evidence file exactly once."""

        with destination.open("x", encoding="utf-8") as output:
            output.write(text)
            output.flush()
            os.fsync(output.fileno())

    def _as_text(self, value: object) -> str:
        """Normalizes timeout output without losing captured bytes."""

        if value is None:
            return ""
        if isinstance(value, bytes):
            return value.decode("utf-8", errors="replace")
        return str(value)

    def _utc_now(self) -> str:
        """Returns one timezone-explicit UTC timestamp."""

        return datetime.now(timezone.utc).isoformat()
