"""Durable file primitives: every write the store makes goes through here (atomic replace, append + fsync).

POSIX gives the ordering guarantees the write protocol needs (``rename`` is atomic, a directory ``fsync`` makes the
rename durable); on Windows ``os.replace`` is atomic and a directory cannot be fsynced, which is skipped.
"""

from __future__ import annotations

import contextlib
import os
import secrets
from pathlib import Path

__all__ = ["append_durable", "fsync_dir", "read_or_none", "write_atomic"]


def fsync_dir(path: str | os.PathLike[str]) -> None:
    if os.name == "nt":  # pragma: no cover
        return
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def write_atomic(path: Path, data: bytes, *, mode: int = 0o644, durable: bool = True) -> None:
    """Replace ``path`` with ``data``: a reader sees the old file or the new one, never a mix."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{secrets.token_hex(4)}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            if durable:
                os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(tmp)
        raise
    if durable:
        fsync_dir(path.parent)


def append_durable(path: Path, data: bytes) -> None:
    """Append ``data`` to ``path`` (created if missing) and fsync the file (and the directory when it is new)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    new = not path.exists()
    fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
    try:
        view = memoryview(data)
        while view:
            n = os.write(fd, view)
            view = view[n:]
        os.fsync(fd)
    finally:
        os.close(fd)
    if new:
        fsync_dir(path.parent)


def read_or_none(path: Path) -> bytes | None:
    try:
        return path.read_bytes()
    except (FileNotFoundError, NotADirectoryError):
        return None
