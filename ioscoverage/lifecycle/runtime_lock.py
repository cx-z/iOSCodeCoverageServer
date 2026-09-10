"""Advisory process lock shared by the HTTP server and destructive maintenance."""

import fcntl
import os
import stat
from pathlib import Path
from typing import Optional, Tuple


RUNTIME_LOCK_LEASE_FD_ENV = "IOS_COVERAGE_RUNTIME_LOCK_LEASE_FD"


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

    @property
    def is_acquired(self) -> bool:
        """Reports whether this exact owner still holds its lock descriptor."""

        return self._descriptor is not None

    @property
    def lease_descriptor(self) -> int:
        """Returns the acquired descriptor for one controlled child inheritance."""

        if self._descriptor is None:
            raise RuntimeLockError("runtime lock must be acquired before creating a child lease")
        return self._descriptor

    def __enter__(self) -> "RuntimeProcessLock":
        self.acquire()
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        self.release()


def inherited_runtime_lock_descriptors() -> Tuple[int, ...]:
    """Returns the validated runtime-lock lease inherited by an analysis process."""

    raw_value = os.environ.get(RUNTIME_LOCK_LEASE_FD_ENV)
    if raw_value is None:
        return ()
    try:
        descriptor = int(raw_value)
        descriptor_stat = os.fstat(descriptor)
    except (TypeError, ValueError, OSError) as error:
        raise RuntimeLockError("inherited runtime lock lease is invalid") from error
    if descriptor < 0 or not stat.S_ISREG(descriptor_stat.st_mode):
        raise RuntimeLockError("inherited runtime lock lease is invalid")
    return (descriptor,)


def validate_inherited_runtime_lock(expected_path: Path) -> None:
    """Confirms an inherited lease refers to the configured runtime lock file."""

    descriptors = inherited_runtime_lock_descriptors()
    if not descriptors:
        return
    try:
        descriptor_stat = os.fstat(descriptors[0])
        path_stat = os.stat(str(expected_path.resolve()), follow_symlinks=False)
    except OSError as error:
        raise RuntimeLockError("inherited runtime lock lease is invalid") from error
    if (
        descriptor_stat.st_dev != path_stat.st_dev
        or descriptor_stat.st_ino != path_stat.st_ino
    ):
        raise RuntimeLockError("inherited runtime lock lease does not match runtime root")
