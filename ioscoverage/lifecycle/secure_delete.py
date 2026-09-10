"""Quarantine-based deletion confined to one already opened runtime root."""

import errno
import hashlib
import os
import stat
import uuid
from pathlib import Path, PurePath
from typing import List, Optional, Sequence, Tuple


TreeEntry = Tuple[str, str, int, str]


class SecureDeletionError(RuntimeError):
    """Reports a bounded containment or identity failure without exposing a path."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


class SecureRuntimeDeletion:
    """Atomically isolates candidates before validating and removing their exact bytes."""

    def __init__(self, runtime_root: Path):
        self.runtime_root = Path(runtime_root)
        self._root_descriptor: Optional[int] = None
        self._quarantine_parent_descriptor: Optional[int] = None
        self._quarantine_descriptor: Optional[int] = None
        self._quarantine_name: Optional[str] = None
        self._directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        if hasattr(os, "O_CLOEXEC"):
            self._directory_flags |= os.O_CLOEXEC

    def __enter__(self) -> "SecureRuntimeDeletion":
        """Opens the trusted root and creates one private quarantine below analysis."""

        if not self.runtime_root.is_absolute():
            raise SecureDeletionError(
                "cleanup_root_unsafe", "runtime_root must be an absolute path"
            )
        try:
            self._root_descriptor = os.open(str(self.runtime_root), self._directory_flags)
            self._quarantine_parent_descriptor = self._open_directory(
                "analysis", self._root_descriptor
            )
            self._quarantine_name = ".cleanup-quarantine-" + uuid.uuid4().hex
            os.mkdir(
                self._quarantine_name,
                mode=0o700,
                dir_fd=self._quarantine_parent_descriptor,
            )
            self._quarantine_descriptor = self._open_directory(
                self._quarantine_name, self._quarantine_parent_descriptor
            )
            os.fsync(self._quarantine_parent_descriptor)
        except (OSError, SecureDeletionError) as error:
            self._close_descriptors()
            if isinstance(error, SecureDeletionError):
                raise
            raise SecureDeletionError(
                "cleanup_root_unsafe", "cleanup quarantine could not be created safely"
            ) from error
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        """Removes an empty quarantine or preserves nonempty evidence after an abort."""

        quarantine = self._quarantine_descriptor
        parent = self._quarantine_parent_descriptor
        name = self._quarantine_name
        if quarantine is not None:
            os.close(quarantine)
            self._quarantine_descriptor = None
        cleanup_error: Optional[OSError] = None
        if parent is not None and name is not None:
            try:
                os.rmdir(name, dir_fd=parent)
                os.fsync(parent)
            except OSError as error:
                if error.errno not in (errno.ENOTEMPTY, errno.EEXIST):
                    cleanup_error = error
        self._close_descriptors()
        if cleanup_error is not None and exc_type is None:
            raise SecureDeletionError(
                "cleanup_delete_failed", "cleanup quarantine could not be finalized safely"
            ) from cleanup_error

    def verify_file(
        self, relative_path: Path, expected_size: int, expected_sha256: str
    ) -> None:
        """Hashes a regular file reached through no-follow directory descriptors."""

        parent, name = self._open_parent(relative_path)
        try:
            self._verify_file_entry(parent, name, expected_size, expected_sha256)
        finally:
            os.close(parent)

    def verify_tree(
        self, relative_path: Path, expected_entries: Sequence[TreeEntry]
    ) -> None:
        """Checks an exact directory tree without following any path component."""

        parent, name = self._open_parent(relative_path)
        try:
            self._verify_tree_entry(parent, name, expected_entries)
        finally:
            os.close(parent)

    def delete_file(
        self, relative_path: Path, expected_size: int, expected_sha256: str
    ) -> None:
        """Moves one name into private quarantine, then verifies and deletes that object."""

        quarantined_name = self._move_to_quarantine(relative_path)
        quarantine = self._require_quarantine_descriptor()
        self._verify_file_entry(
            quarantine, quarantined_name, expected_size, expected_sha256
        )
        try:
            os.unlink(quarantined_name, dir_fd=quarantine)
            os.fsync(quarantine)
        except OSError as error:
            raise SecureDeletionError(
                "cleanup_delete_failed", "cleanup file could not be removed safely"
            ) from error

    def delete_tree(
        self, relative_path: Path, expected_entries: Sequence[TreeEntry]
    ) -> None:
        """Moves one tree into private quarantine, verifies it, then removes it recursively."""

        quarantined_name = self._move_to_quarantine(relative_path)
        quarantine = self._require_quarantine_descriptor()
        self._verify_tree_entry(quarantine, quarantined_name, expected_entries)
        self._delete_directory(quarantine, quarantined_name)

    def _move_to_quarantine(self, relative_path: Path) -> str:
        """Atomically detaches the final entry from its public runtime location."""

        source_parent, source_name = self._open_parent(relative_path)
        quarantine = self._require_quarantine_descriptor()
        quarantined_name = "candidate-" + uuid.uuid4().hex
        try:
            os.rename(
                source_name,
                quarantined_name,
                src_dir_fd=source_parent,
                dst_dir_fd=quarantine,
            )
            os.fsync(source_parent)
            os.fsync(quarantine)
        except OSError as error:
            raise SecureDeletionError(
                "cleanup_path_changed", "cleanup candidate could not be isolated safely"
            ) from error
        finally:
            os.close(source_parent)
        return quarantined_name

    def _open_parent(self, relative_path: Path) -> Tuple[int, str]:
        """Opens every ancestor with O_NOFOLLOW starting from the fixed root descriptor."""

        parts = self._relative_parts(relative_path)
        current = os.dup(self._require_root_descriptor())
        try:
            for part in parts[:-1]:
                child = self._open_directory(part, current)
                os.close(current)
                current = child
            return current, parts[-1]
        except Exception:
            os.close(current)
            raise

    def _open_directory(self, name: str, parent: int) -> int:
        """Opens one real directory entry without following a symbolic link."""

        try:
            return os.open(name, self._directory_flags, dir_fd=parent)
        except OSError as error:
            raise SecureDeletionError(
                "cleanup_path_changed", "cleanup ancestor is missing or unsafe"
            ) from error

    def _verify_file_entry(
        self, parent: int, name: str, expected_size: int, expected_sha256: str
    ) -> None:
        """Requires the named regular file to match an exact byte identity."""

        actual_size, actual_sha256 = self._file_identity(parent, name)
        if actual_size != expected_size or actual_sha256 != expected_sha256:
            raise SecureDeletionError(
                "cleanup_candidate_identity_mismatch", "cleanup candidate bytes changed"
            )

    def _file_identity(self, parent: int, name: str) -> Tuple[int, str]:
        """Hashes an opened file and proves the parent entry still names the same inode."""

        flags = os.O_RDONLY | os.O_NOFOLLOW
        if hasattr(os, "O_CLOEXEC"):
            flags |= os.O_CLOEXEC
        try:
            descriptor = os.open(name, flags, dir_fd=parent)
        except OSError as error:
            raise SecureDeletionError(
                "cleanup_candidate_identity_mismatch",
                "cleanup candidate is missing or unsafe",
            ) from error
        try:
            before = os.fstat(descriptor)
            if not stat.S_ISREG(before.st_mode):
                raise SecureDeletionError(
                    "cleanup_candidate_identity_mismatch",
                    "cleanup candidate is not a regular file",
                )
            digest = hashlib.sha256()
            while True:
                chunk = os.read(descriptor, 1024 * 1024)
                if not chunk:
                    break
                digest.update(chunk)
            after = os.fstat(descriptor)
            if (
                before.st_dev != after.st_dev
                or before.st_ino != after.st_ino
                or before.st_size != after.st_size
            ):
                raise SecureDeletionError(
                    "cleanup_candidate_identity_mismatch", "cleanup candidate bytes changed"
                )
            try:
                current = os.stat(name, dir_fd=parent, follow_symlinks=False)
            except OSError as error:
                raise SecureDeletionError(
                    "cleanup_candidate_identity_mismatch", "cleanup candidate entry changed"
                ) from error
            if current.st_dev != after.st_dev or current.st_ino != after.st_ino:
                raise SecureDeletionError(
                    "cleanup_candidate_identity_mismatch", "cleanup candidate entry changed"
                )
            return after.st_size, digest.hexdigest()
        finally:
            os.close(descriptor)

    def _verify_tree_entry(
        self, parent: int, name: str, expected_entries: Sequence[TreeEntry]
    ) -> None:
        """Requires a directory and every descendant to equal one planned tree identity."""

        directory = self._open_directory(name, parent)
        try:
            opened = os.fstat(directory)
            actual = tuple(self._collect_tree_entries(directory, ""))
            current = os.stat(name, dir_fd=parent, follow_symlinks=False)
            if current.st_dev != opened.st_dev or current.st_ino != opened.st_ino:
                raise SecureDeletionError(
                    "cleanup_candidate_identity_mismatch", "cleanup directory entry changed"
                )
        except OSError as error:
            raise SecureDeletionError(
                "cleanup_candidate_identity_mismatch", "cleanup directory entry changed"
            ) from error
        finally:
            os.close(directory)
        if actual != tuple(expected_entries):
            raise SecureDeletionError(
                "cleanup_candidate_identity_mismatch", "cleanup directory tree changed"
            )

    def _collect_tree_entries(self, directory: int, prefix: str) -> List[TreeEntry]:
        """Returns a stable, byte-addressed tree without following special entries."""

        result: List[TreeEntry] = []
        try:
            entries = sorted(os.listdir(directory))
        except OSError as error:
            raise SecureDeletionError(
                "cleanup_candidate_identity_mismatch",
                "cleanup directory could not be inspected safely",
            ) from error
        for name in entries:
            relative = name if not prefix else prefix + "/" + name
            try:
                metadata = os.stat(name, dir_fd=directory, follow_symlinks=False)
            except OSError as error:
                raise SecureDeletionError(
                    "cleanup_candidate_identity_mismatch", "cleanup directory entry changed"
                ) from error
            if stat.S_ISDIR(metadata.st_mode):
                child = self._open_directory(name, directory)
                try:
                    opened = os.fstat(child)
                    if opened.st_dev != metadata.st_dev or opened.st_ino != metadata.st_ino:
                        raise SecureDeletionError(
                            "cleanup_candidate_identity_mismatch",
                            "cleanup directory entry changed",
                        )
                    result.append((relative, "directory", 0, ""))
                    result.extend(self._collect_tree_entries(child, relative))
                finally:
                    os.close(child)
            elif stat.S_ISREG(metadata.st_mode):
                size, sha256 = self._file_identity(directory, name)
                result.append((relative, "file", size, sha256))
            else:
                raise SecureDeletionError(
                    "cleanup_path_changed", "cleanup directory contains an unsafe entry"
                )
        return result

    def _delete_directory(self, parent: int, name: str) -> None:
        """Removes one quarantined directory without following any descendant entry."""

        directory = self._open_directory(name, parent)
        try:
            entries = os.listdir(directory)
            for entry in entries:
                metadata = os.stat(entry, dir_fd=directory, follow_symlinks=False)
                if stat.S_ISDIR(metadata.st_mode):
                    self._delete_directory(directory, entry)
                elif stat.S_ISREG(metadata.st_mode):
                    os.unlink(entry, dir_fd=directory)
                else:
                    raise SecureDeletionError(
                        "cleanup_path_changed", "cleanup directory contains an unsafe entry"
                    )
            os.fsync(directory)
        except SecureDeletionError:
            raise
        except OSError as error:
            raise SecureDeletionError(
                "cleanup_delete_failed", "cleanup directory could not be removed safely"
            ) from error
        finally:
            os.close(directory)
        try:
            os.rmdir(name, dir_fd=parent)
            os.fsync(parent)
        except OSError as error:
            raise SecureDeletionError(
                "cleanup_delete_failed", "cleanup directory could not be removed safely"
            ) from error

    def _relative_parts(self, relative_path: Path) -> Tuple[str, ...]:
        """Rejects absolute, empty, dot, and parent-traversal inputs before traversal."""

        path = PurePath(relative_path)
        parts = path.parts
        if path.is_absolute() or not parts or any(part in ("", ".", "..") for part in parts):
            raise SecureDeletionError(
                "cleanup_path_escaped", "cleanup target escaped runtime_root"
            )
        return parts

    def _require_root_descriptor(self) -> int:
        """Requires use inside the context that owns the trusted root descriptor."""

        if self._root_descriptor is None:
            raise SecureDeletionError(
                "cleanup_root_unsafe", "secure cleanup root is not open"
            )
        return self._root_descriptor

    def _require_quarantine_descriptor(self) -> int:
        """Requires a private quarantine owned by this cleanup invocation."""

        if self._quarantine_descriptor is None:
            raise SecureDeletionError(
                "cleanup_root_unsafe", "secure cleanup quarantine is not open"
            )
        return self._quarantine_descriptor

    def _close_descriptors(self) -> None:
        """Closes any descriptors left open after setup, success, or failure."""

        if self._quarantine_descriptor is not None:
            os.close(self._quarantine_descriptor)
            self._quarantine_descriptor = None
        if self._quarantine_parent_descriptor is not None:
            os.close(self._quarantine_parent_descriptor)
            self._quarantine_parent_descriptor = None
        if self._root_descriptor is not None:
            os.close(self._root_descriptor)
            self._root_descriptor = None
