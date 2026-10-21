"""Wait until one fd is readable or writable, at any fd number.

``select.select`` refuses an fd at or above ``FD_SETSIZE`` (1024): ``ValueError: filedescriptor out of range in
select()``. A long-running process that holds many fds (the host, a runner with many terminals) gets there, and then
every wait on a new fd fails. These helpers use :class:`selectors.DefaultSelector` (epoll, kqueue or poll), which has
no such limit. Production code waits through here; ``tests/test_fdwait.py`` keeps ``select.select`` out of ``src``.
"""

from __future__ import annotations

import selectors
from typing import IO, Any

FileLike = int | IO[Any] | Any  # an fd, or anything with ``fileno()`` (a file, a socket)


def _wait(target: FileLike, events: int, timeout: float | None) -> bool:
    with selectors.DefaultSelector() as sel:
        sel.register(target, events)
        return bool(sel.select(None if timeout is None else max(0.0, timeout)))


def readable(target: FileLike, timeout: float | None) -> bool:
    """Whether ``target`` has something to read (or reached end of file) within ``timeout`` seconds."""
    return _wait(target, selectors.EVENT_READ, timeout)


def ready(targets: list[FileLike], timeout: float | None) -> list[FileLike]:
    """The members of ``targets`` that have something to read within ``timeout`` seconds (``select``'s first list)."""
    with selectors.DefaultSelector() as sel:
        for i, t in enumerate(targets):
            sel.register(t, selectors.EVENT_READ, i)
        hits = {key.data for key, _ in sel.select(None if timeout is None else max(0.0, timeout))}
    return [t for i, t in enumerate(targets) if i in hits]


def writable(target: FileLike, timeout: float | None) -> bool:
    """Whether ``target`` takes a write within ``timeout`` seconds."""
    return _wait(target, selectors.EVENT_WRITE, timeout)


__all__ = ["readable", "ready", "writable"]
