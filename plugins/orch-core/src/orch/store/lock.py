"""The workspace lock: one cross-process lock per file (``fcntl.flock``; ``msvcrt.locking`` on Windows).

Within a process the lock is re-entrant and shared by every :class:`FileLock` of the same path (two ``flock`` file
descriptions of one file would deadlock a process against itself). The lock file is never deleted.
"""

from __future__ import annotations

import os
import threading
import time
from pathlib import Path

from .errors import StoreError

__all__ = ["FileLock", "LockTimeout"]

try:  # POSIX
    import fcntl
except ImportError:  # pragma: no cover - Windows
    fcntl = None  # type: ignore[assignment]
    import msvcrt

_registry: dict[str, _Held] = {}
_registry_guard = threading.Lock()


class LockTimeout(StoreError):
    """Another process (or thread) holds the workspace lock for longer than the timeout: ``store.busy``, retryable."""

    def __init__(self, detail: str = "the workspace lock is held by another process") -> None:
        super().__init__("store.busy", detail)


class _Held:
    def __init__(self, path: str) -> None:
        self.path = path
        self.rlock = threading.RLock()
        self.depth = 0
        self.fd: int | None = None


def _flock(fd: int, timeout: float | None) -> None:
    deadline = None if timeout is None else time.monotonic() + timeout
    if fcntl is not None:
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return
            except BlockingIOError:
                if deadline is not None and time.monotonic() >= deadline:
                    raise LockTimeout() from None
                time.sleep(0.002)
    while True:  # pragma: no cover - Windows
        try:
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            return
        except OSError:
            if deadline is not None and time.monotonic() >= deadline:
                raise LockTimeout() from None
            time.sleep(0.01)


def _funlock(fd: int) -> None:
    if fcntl is not None:
        fcntl.flock(fd, fcntl.LOCK_UN)
    else:  # pragma: no cover - Windows
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)


class FileLock:
    """``with FileLock(path):`` holds the exclusive lock; nesting in one thread is fine, other threads and other
    processes wait (or raise :class:`LockTimeout` after ``timeout`` seconds)."""

    def __init__(self, path: str | os.PathLike[str], *, timeout: float | None = None) -> None:
        self.path = Path(path)
        self.timeout = timeout
        key = os.path.abspath(self.path)
        with _registry_guard:
            self._held = _registry.setdefault(key, _Held(key))

    def acquire(self) -> None:
        h = self._held
        if not h.rlock.acquire(timeout=-1 if self.timeout is None else self.timeout):
            raise LockTimeout("the workspace lock is held by another thread")
        try:
            if h.depth == 0:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                fd = os.open(self.path, os.O_RDWR | os.O_CREAT | getattr(os, "O_BINARY", 0), 0o600)
                try:
                    _flock(fd, self.timeout)
                except BaseException:
                    os.close(fd)
                    raise
                h.fd = fd
            h.depth += 1
        except BaseException:
            h.rlock.release()
            raise

    def release(self) -> None:
        h = self._held
        h.depth -= 1
        try:
            if h.depth == 0 and h.fd is not None:
                try:
                    _funlock(h.fd)
                finally:
                    os.close(h.fd)
                    h.fd = None
        finally:
            h.rlock.release()

    def __enter__(self) -> FileLock:
        self.acquire()
        return self

    def __exit__(self, *exc: object) -> None:
        self.release()
