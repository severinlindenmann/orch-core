"""Durable file primitives: every write the store makes goes through here (atomic replace, append + fsync).

POSIX gives the ordering guarantees the write protocol needs (``rename`` is atomic, a directory ``fsync`` makes the
rename durable). Platform notes (ticket-format §2.1):

* **macOS**: ``os.fsync`` does not flush the drive cache; the commit (the log append) uses ``F_FULLFSYNC``.
* **Windows** (best effort): files are opened ``O_BINARY`` (no CRLF translation), a directory cannot be fsynced,
  ``os.replace`` is retried a few times on ``PermissionError`` (a reader, an indexer or antivirus holds the target).
* **No ``O_NOFOLLOW``** (Windows): a file is opened, then ``fstat`` and ``lstat`` must agree (same file, not a link);
  otherwise it counts as absent or the open fails (closed, never open).
"""

from __future__ import annotations

import contextlib
import errno
import json
import os
import secrets
import stat
import sys
import time
from pathlib import Path

__all__ = ["append_durable", "fsync_dir", "loads", "read_or_none", "replace", "tail_bytes", "write_atomic"]

_BIN = getattr(os, "O_BINARY", 0)
_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_RETRY = os.name == "nt"  # a busy target (a reader, an indexer) is retried only where that can happen


def _full_fsync(fd: int) -> None:
    if sys.platform == "darwin":
        import fcntl

        try:
            fcntl.fcntl(fd, fcntl.F_FULLFSYNC)
            return
        except OSError:  # a file system without F_FULLFSYNC falls back to fsync
            pass
    os.fsync(fd)


def fsync_dir(path: str | os.PathLike[str]) -> None:
    if os.name == "nt":  # pragma: no cover
        return
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def replace(src: str | os.PathLike[str], dst: str | os.PathLike[str]) -> None:
    """``os.replace``, retried for up to about 0.5 s on ``PermissionError`` (Windows: the target is open elsewhere)."""
    for attempt in range(25):
        try:
            os.replace(src, dst)
            return
        except PermissionError:
            if not _RETRY or attempt == 24:
                raise
            time.sleep(0.02)  # pragma: no cover


def write_atomic(path: Path, data: bytes, *, mode: int = 0o644, durable: bool = True) -> None:
    """Replace ``path`` with ``data``: a reader sees the old file or the new one, never a mix."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{secrets.token_hex(4)}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | _BIN, mode)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            if durable:
                os.fsync(f.fileno())
        replace(tmp, path)
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(tmp)
        raise
    if durable:
        fsync_dir(path.parent)


def open_nofollow(path: Path, flags: int, mode: int = 0o644) -> int:
    """``os.open`` that never follows a symlink; where the platform has no ``O_NOFOLLOW`` the opened file must be the
    file ``lstat`` sees (and not a link), else ``OSError(ELOOP)``."""
    fd = os.open(path, flags | _BIN | _NOFOLLOW, mode)
    if not _NOFOLLOW:  # pragma: no cover - Windows
        try:
            a, b = os.fstat(fd), os.lstat(path)
            if stat.S_ISLNK(b.st_mode) or (a.st_ino, a.st_dev) != (b.st_ino, b.st_dev):
                raise OSError(errno.ELOOP, "not the file lstat sees (a link?)", str(path))
        except BaseException:
            os.close(fd)
            raise
    return fd


def append_durable(path: Path, data: bytes, *, commit: bool = False) -> None:
    """Append ``data`` to ``path`` (created if missing) and fsync the file (and the directory when it is new).
    ``commit=True`` is the log append: it asks the drive to flush its cache too (``F_FULLFSYNC`` on macOS)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    new = not os.path.lexists(path)
    fd = open_nofollow(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT)
    try:
        view = memoryview(data)
        while view:
            n = os.write(fd, view)
            view = view[n:]
        if commit:
            _full_fsync(fd)
        else:
            os.fsync(fd)
    finally:
        os.close(fd)
    if new:
        fsync_dir(path.parent)


MAX_READ = 16 * 1024 * 1024


def read_or_none(path: Path, limit: int | None = MAX_READ) -> bytes | None:
    """The bytes of ``path``; None if it is missing or a symlink (a link is never followed: it counts as absent). At
    most ``limit`` + 1 bytes are read: a larger file comes back one byte too long, which no comparison or parse accepts,
    and an agent-written file cannot make the store allocate more (``limit=None`` reads all, for our own files)."""
    try:
        fd = open_nofollow(path, os.O_RDONLY)
    except (FileNotFoundError, NotADirectoryError):
        return None
    except OSError as e:
        if e.errno in (errno.ELOOP, errno.EMLINK):
            return None
        raise
    with os.fdopen(fd, "rb") as f:
        return f.read() if limit is None else f.read(limit + 1)


def loads(raw: bytes | str) -> object:
    """``json.loads`` for data an agent may have written: deep nesting is a ``ValueError``, not a ``RecursionError``."""
    try:
        return json.loads(raw)
    except RecursionError:
        raise ValueError("nested too deeply") from None


def tail_bytes(path: Path, n: int) -> tuple[int, bytes]:
    """``(size, last n bytes)`` of a file (a symlink or a missing file is ``(0, b"")``)."""
    try:
        fd = open_nofollow(path, os.O_RDONLY)
    except OSError:
        return 0, b""
    with os.fdopen(fd, "rb") as f:
        size = f.seek(0, os.SEEK_END)
        f.seek(max(0, size - n))
        return size, f.read(n)
