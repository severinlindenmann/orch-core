"""The workspace lock: one cross-process lock per file (``orch.oslock``: ``fcntl.flock``, ``LockFileEx`` on Windows).

Within a process the lock is re-entrant and shared by every :class:`FileLock` of the same lock file, by inode (two
``flock`` file descriptions of one file would deadlock a process against itself). The lock file is never deleted.
"""

from __future__ import annotations

import os
import threading
import time
from collections.abc import Callable
from pathlib import Path

from .errors import StoreError

__all__ = ["FileLock", "LockTimeout"]

from .. import oslock

_registry: dict[tuple[int, int], _Held] = {}
_registry_guard = threading.Lock()


class LockTimeout(StoreError):
    """Another process (or thread) holds the workspace lock for longer than the timeout: ``store.busy``, retryable."""

    def __init__(self, detail: str = "the workspace lock is held by another process") -> None:
        super().__init__("store.busy", detail)


class _Held:
    def __init__(self, key: tuple[int, int]) -> None:
        self.key = key
        self.rlock = threading.RLock()
        self.depth = 0
        self.fd: int | None = None


def _flock(fd: int, timeout: float | None) -> None:
    deadline = None if timeout is None else time.monotonic() + timeout
    while True:
        try:
            oslock.flock(fd, oslock.LOCK_EX | oslock.LOCK_NB)
            return
        except BlockingIOError:
            if deadline is not None and time.monotonic() >= deadline:
                raise LockTimeout() from None
            time.sleep(0.002)


def _funlock(fd: int) -> None:
    oslock.flock(fd, oslock.LOCK_UN)


class FileLock:
    """``with FileLock(path):`` holds the exclusive lock; nesting in one thread is fine, other threads and other
    processes wait (or raise :class:`LockTimeout` after ``timeout`` seconds).

    Re-entry is keyed by the lock **file** (``st_dev``, ``st_ino`` of the opened fd), never by its pathname: a second
    :class:`FileLock` in the same thread re-enters only when it opened the very file already held (#564). ``opener``
    opens the file (the store's: below its pinned root); ``verify(fd)``, after the ``flock``, says the locked file is
    still the one the name points to (the store compares it with ``.state/lock`` below the revalidated ``.state``).
    A file replaced in between is released and opened again once; a second mismatch refuses (``validation.path``).
    Without an ``opener`` both default to the path (an ``lstat`` of the path must be the locked file)."""

    def __init__(
        self,
        path: str | os.PathLike[str],
        *,
        timeout: float | None = None,
        opener: Callable[[], int] | None = None,
        verify: Callable[[int], bool] | None = None,
    ) -> None:
        self.path = Path(path)
        self.timeout = timeout
        self._opener = opener
        self._verify = verify if verify is not None else (self._same_as_path if opener is None else None)
        self._tl = threading.local()

    def _open(self) -> int:
        if self._opener is not None:
            return self._opener()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        return os.open(self.path, os.O_RDWR | os.O_CREAT | getattr(os, "O_BINARY", 0), 0o600)

    def _same_as_path(self, fd: int) -> bool:
        try:
            a, b = os.fstat(fd), os.stat(self.path, follow_symlinks=False)
        except OSError:
            return False
        return (a.st_dev, a.st_ino) == (b.st_dev, b.st_ino)

    def held_by_me(self) -> bool:
        """This thread holds the lock through this object (R6a: the store's ``lock_held``)."""
        return bool(self._stack())

    def _stack(self) -> list[_Held]:
        st = getattr(self._tl, "stack", None)
        if st is None:
            st = self._tl.stack = []
        return st

    def acquire(self) -> None:
        stack = self._stack()
        if stack:  # this thread holds it through this object already
            h = stack[-1]
            h.rlock.acquire()
            h.depth += 1
            stack.append(h)
            return
        for attempt in (0, 1):
            fd = self._open()
            try:
                st = os.fstat(fd)
            except BaseException:
                os.close(fd)
                raise
            key = (st.st_dev, st.st_ino)
            with _registry_guard:
                h = _registry.setdefault(key, _Held(key))
            if not h.rlock.acquire(timeout=-1 if self.timeout is None else self.timeout):
                os.close(fd)
                raise LockTimeout("the workspace lock is held by another thread")
            if h.depth > 0:  # this thread holds this very file through another FileLock: re-enter
                os.close(fd)
                h.depth += 1
                stack.append(h)
                return
            try:
                _flock(fd, self.timeout)
                ok = self._verify is None or self._verify(fd)
            except BaseException:
                os.close(fd)
                h.rlock.release()
                raise
            if ok:
                h.fd, h.depth = fd, 1
                stack.append(h)
                return
            _funlock(fd)
            os.close(fd)
            h.rlock.release()
            if attempt == 1:
                raise StoreError("validation.path", f"{self.path.name}: the lock file was replaced while it was taken")
            # the name points to another file now: take that one
        raise AssertionError("unreachable")  # pragma: no cover

    def release(self) -> None:
        h = self._stack().pop()
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
