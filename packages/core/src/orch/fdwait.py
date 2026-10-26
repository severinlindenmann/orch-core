"""Wait until one fd is readable or writable, at any fd number.

``select.select`` refuses an fd at or above ``FD_SETSIZE`` (1024): ``ValueError: filedescriptor out of range in
select()``. A long-running process that holds many fds (the host, a runner with many terminals) gets there, and then
every wait on a new fd fails. These helpers use :class:`selectors.DefaultSelector` (epoll, kqueue or poll), which has
no such limit. Production code waits through here; ``tests/test_fdwait.py`` keeps ``select.select`` out of ``src``
everywhere except this module.

kqueue (macOS) cannot watch some fds, notably ``/dev/tty`` (``OSError`` EINVAL on register, or EPERM). For those the
wait falls back to ``select.select`` when the fd is below ``FD_SETSIZE``, else to ``select.poll`` where it exists
(``POLLNVAL``, an fd poll cannot watch either, raises ``OSError`` EINVAL); any other error is raised.
"""

from __future__ import annotations

import errno
import select
import selectors
from typing import IO, Any

FileLike = int | IO[Any] | Any  # an fd, or anything with ``fileno()`` (a file, a socket)

_FD_SETSIZE = 1024
_FALLBACK_ERRNOS = (errno.EINVAL, errno.EPERM)


def _fd(target: FileLike) -> int:
    return target if isinstance(target, int) else int(target.fileno())


def _fallback(targets: list[FileLike], events: int, timeout: float | None) -> list[FileLike]:
    """The targets ready for ``events`` (``EVENT_READ`` or ``EVENT_WRITE``), through ``select.select`` or ``poll``."""
    fds = [_fd(t) for t in targets]
    if all(fd < _FD_SETSIZE for fd in fds):
        r, w = ([*fds], []) if events == selectors.EVENT_READ else ([], [*fds])
        rr, ww, _ = select.select(r, w, [], timeout)
        hit = set(rr) | set(ww)
    elif hasattr(select, "poll"):
        poller = select.poll()
        mask = select.POLLIN if events == selectors.EVENT_READ else select.POLLOUT
        for fd in fds:
            poller.register(fd, mask)
        hit = set()
        for fd, ev in poller.poll(None if timeout is None else timeout * 1000):
            if ev & select.POLLNVAL:  # poll cannot watch it either (macOS /dev/tty): not "ready"
                raise OSError(errno.EINVAL, f"cannot wait on fd {fd}")
            hit.add(fd)
    else:  # pragma: no cover - every supported platform has poll
        raise OSError(errno.EINVAL, "no way to wait on an fd above FD_SETSIZE")
    return [t for t, fd in zip(targets, fds, strict=True) if fd in hit]


def _wait(targets: list[FileLike], events: int, timeout: float | None) -> list[FileLike]:
    timeout = None if timeout is None else max(0.0, timeout)
    try:
        with selectors.DefaultSelector() as sel:
            for i, t in enumerate(targets):
                sel.register(t, events, i)
            hits = {key.data for key, _ in sel.select(timeout)}
        return [t for i, t in enumerate(targets) if i in hits]
    except OSError as e:
        if e.errno not in _FALLBACK_ERRNOS:
            raise
    return _fallback(targets, events, timeout)


def readable(target: FileLike, timeout: float | None) -> bool:
    """Whether ``target`` has something to read (or reached end of file) within ``timeout`` seconds."""
    return bool(_wait([target], selectors.EVENT_READ, timeout))


def ready(targets: list[FileLike], timeout: float | None) -> list[FileLike]:
    """The members of ``targets`` that have something to read within ``timeout`` seconds (``select``'s first list)."""
    return _wait(list(targets), selectors.EVENT_READ, timeout)


def writable(target: FileLike, timeout: float | None) -> bool:
    """Whether ``target`` takes a write within ``timeout`` seconds."""
    return bool(_wait([target], selectors.EVENT_WRITE, timeout))


__all__ = ["readable", "ready", "writable"]
