"""Advisory process lock shared by the HTTP server and destructive maintenance."""

import fcntl
import os
from pathlib import Path
from typing import Optional


class RuntimeLockError(RuntimeError):
    """Reports that another process owns the configured runtime root."""


class RuntimeProcessLock:
    """Holds a nonblocking exclusive lock for one server or maintenance process."""

    def __init__(self, path: Path):
        self.path = path.resolve()
        self._descriptor: Optional[int] = None

    def acquire(self) -> None:
        """Acquires the runtime lock without waiting or changing another process."""

        self.path.parent.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(str(self.path), os.O_CREAT | os.O_RDWR, 0o600)
        try:
            os.fchmod(descriptor, 0o600)
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (BlockingIOError, OSError) as error:
            os.close(descriptor)
            raise RuntimeLockError("runtime is already owned by a running server or maintenance process") from error
        self._descriptor = descriptor

    def release(self) -> None:
        """Releases this process's advisory lock without deleting its state file."""

        if self._descriptor is None:
            return
        descriptor, self._descriptor = self._descriptor, None
        try:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)

    def __enter__(self) -> "RuntimeProcessLock":
        self.acquire()
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        self.release()
