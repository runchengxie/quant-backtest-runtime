"""Fail-closed POSIX filesystem barriers for result publication."""

from __future__ import annotations

import errno
import os
from pathlib import Path


def sync_directory(path: Path) -> None:
    """Persist directory entries; unsupported platforms must not claim success."""
    if os.name != "posix":
        raise OSError(errno.ENOTSUP, "durable result publication requires POSIX fsync")
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def prepare_result_root(path: Path) -> None:
    """Create and persist root ancestry before it can hold a published result."""
    path.mkdir(parents=True, exist_ok=True)
    # Include existing ancestors: a concurrent creator may not have synced them.
    for directory in (path, *path.parents):
        sync_directory(directory)


def sync_result_tree(path: Path) -> None:
    """Persist closed payload files and their directories from leaves upward."""
    for directory, _children, files in os.walk(path, topdown=False, onerror=_raise):
        parent = Path(directory)
        for name in files:
            with (parent / name).open("rb") as payload:
                os.fsync(payload.fileno())
        sync_directory(parent)


def _raise(error: OSError) -> None:
    raise error
