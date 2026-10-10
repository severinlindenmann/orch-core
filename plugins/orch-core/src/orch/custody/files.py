"""Shared on-disk helpers for the two file-based backends: atomic exclusive create with mode 0600 and a private
directory. OS-specific calls (``os.chmod`` modes) live here and in ``custody/`` only (portable-custody §4)."""

from __future__ import annotations

import contextlib
import os
from pathlib import Path

from .base import KeyExists, KeyNotFound, check_key_id

__all__ = ["delete_file", "key_path", "private_dir", "write_new"]


def private_dir(directory: str | os.PathLike[str]) -> Path:
    p = Path(directory)
    p.mkdir(parents=True, exist_ok=True)
    with contextlib.suppress(OSError):  # not meaningful on Windows
        os.chmod(p, 0o700)
    return p


def key_path(directory: Path, key_id: str, suffix: str) -> Path:
    return directory / (check_key_id(key_id) + suffix)


def write_new(path: Path, data: bytes) -> None:
    """Create ``path`` exclusively with mode 0600 via a temporary file, so a reader never sees a partial key."""
    if path.exists():
        raise KeyExists(path.name)
    tmp = path.with_name(path.name + f".tmp{os.getpid()}")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        if path.exists():
            raise KeyExists(path.name)
        os.replace(tmp, path)
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(tmp)


def delete_file(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        raise KeyNotFound(path.stem) from None
