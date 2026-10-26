"""Shared on-disk helpers for the two file-based backends: atomic exclusive publish with mode 0600, a private directory
and a permission check on load. OS-specific calls (``os.chmod`` modes, ``os.getuid``) live here and in ``custody/``
only (portable-custody §4)."""

from __future__ import annotations

import contextlib
import os
from pathlib import Path

from .base import CustodyError, KeyExists, KeyNotFound, check_key_id

__all__ = ["check_private", "delete_file", "key_path", "private_dir", "write_new"]


def private_dir(directory: str | os.PathLike[str]) -> Path:
    p = Path(directory)
    p.mkdir(parents=True, exist_ok=True)
    with contextlib.suppress(OSError):  # not meaningful on Windows
        os.chmod(p, 0o700)
    return p


def key_path(directory: Path, key_id: str, suffix: str) -> Path:
    return directory / (check_key_id(key_id) + suffix)


def write_new(path: Path, data: bytes) -> None:
    """Publish ``data`` at ``path`` with mode 0600, atomically and exclusively: the complete file appears at once, and
    if ``path`` exists (created by anyone, at any moment) nothing is replaced and :class:`KeyExists` is raised.

    The data is written to a private temporary file and then *linked* into place (``os.link`` fails if the target
    exists, unlike ``os.replace``); on Windows ``os.rename``, which also refuses to overwrite. A file system without
    hard links falls back to ``O_CREAT | O_EXCL`` on the final name (still exclusive)."""
    tmp = path.with_name(f"{path.name}.tmp{os.getpid()}.{os.urandom(4).hex()}")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        try:
            if os.name == "nt":  # pragma: no cover
                os.rename(tmp, path)
            else:
                os.link(tmp, path)
        except FileExistsError:
            raise KeyExists(path.name) from None
        except OSError:
            if os.name == "nt" or path.exists():  # pragma: no cover
                raise KeyExists(path.name) from None
            _write_exclusive(path, data)
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(tmp)


def _write_exclusive(path: Path, data: bytes) -> None:
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        raise KeyExists(path.name) from None
    with os.fdopen(fd, "wb") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())


def check_private(path: Path) -> None:
    """Refuse a key file that others can read or write, or that another user owns (POSIX; skipped on Windows)."""
    if os.name == "nt":  # pragma: no cover
        return
    try:
        st = os.stat(path)
    except FileNotFoundError:
        raise KeyNotFound(path.stem) from None
    if st.st_mode & 0o077:
        raise CustodyError(f"key file {path.name} is accessible by others (mode {st.st_mode & 0o777:03o}); chmod 600")
    if st.st_uid != os.getuid():
        raise CustodyError(f"key file {path.name} belongs to another user")


def delete_file(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        raise KeyNotFound(path.stem) from None
