"""Wait until a socket is readable or writable, at any fd number (the hub's twin of core ``orch.fdwait``).

``select.select`` refuses an fd at or above ``FD_SETSIZE`` (1024): ``ValueError: filedescriptor out of range in
select()``. A hub holding many connections, or a long test worker, gets there, and then every wait on a new
connection fails and the connection is dropped without a word (CI 30 Oct, #1740: the R4 redial test). The hub only
waits on sockets, which every :class:`selectors.DefaultSelector` (epoll, kqueue, poll) can watch, so there is no
fallback. ``tests/test_fdwait.py`` keeps ``select.select`` out of ``orch_hub``.
"""

from __future__ import annotations

import selectors
from typing import Any


def _wait(sock: Any, events: int, timeout: float | None) -> bool:
    timeout = None if timeout is None else max(0.0, timeout)
    with selectors.DefaultSelector() as sel:
        sel.register(sock, events)
        return bool(sel.select(timeout))


def readable(sock: Any, timeout: float | None) -> bool:
    """Whether ``sock`` has something to read (or reached end of file) within ``timeout`` seconds."""
    return _wait(sock, selectors.EVENT_READ, timeout)


def writable(sock: Any, timeout: float | None) -> bool:
    """Whether ``sock`` takes a write within ``timeout`` seconds."""
    return _wait(sock, selectors.EVENT_WRITE, timeout)
