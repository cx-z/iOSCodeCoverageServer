"""Explicit file types outside instrumented iOS runtime code for MR display."""

from pathlib import PurePosixPath


NON_INSTRUMENTED_SUFFIXES = frozenset({".md", ".pbxproj", ".strings"})
NON_INSTRUMENTED_NAMES = frozenset({"Podfile", "Podfile.lock"})


def is_non_instrumented_file(path):
    """Recognizes documents, Xcode metadata, localization and CocoaPods manifests.

    Missing compiler evidence alone cannot classify other paths, including
    source files with new executable code, as not measurable.
    """

    path = PurePosixPath(path)
    return path.suffix in NON_INSTRUMENTED_SUFFIXES or path.name in NON_INSTRUMENTED_NAMES
