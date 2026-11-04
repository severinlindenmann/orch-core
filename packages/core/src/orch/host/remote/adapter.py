"""The dashboard's side of a remote session, as the host sees it (R3, #1591; design note r3-session-design.md 0, 1).
Named ``adapter``, not ``attach``: the package already has an ``attach`` hook (host start).

One **adapter** per workspace: the dashboard process that terminates the HTTP of the remote sessions in-process (D89).
It attaches with ``remote.attach`` over its own host connection (a person's call, an owner) and from then on

- *receives* the decrypted app frames of the open streams, each stream's ``open`` / ``closed`` notice and, on every
  ``HEAD`` / ``WS_OPEN``, the identity the host computed from its model at that moment; and
- *sends* the app frames of the answers and its stream closes.

Nothing here is a socket: frames cross the host's own connection to that one dashboard, never a listener (D89). The
attachment is a lease: it is refreshed by every ``remote.attach`` call and ends when none comes for :data:`LEASE_S`
(or on :func:`release`), which answers every new ``stream_open`` ``remote.version`` and closes the open sessions.
Another adapter cannot take the slot while the lease is live (``remote.busy``). The ``person`` of the adapter is the
person of the call: a device of any other person never opens a session on it (``remote.device``)."""

from __future__ import annotations

import collections
import secrets
import threading
from typing import Any

from ..errors import HostError

__all__ = ["LEASE_S", "QUEUE_MAX", "Adapter", "attached", "claim", "current", "release"]

LEASE_S = 15.0
QUEUE_MAX = 512  # frames for the adapter that are not yet taken; past it the stream of the newest frame is closed
_ATTR = "_remote_adapter"
_MU = threading.Lock()


class Adapter:
    def __init__(self, person: str, now: float) -> None:
        self.person = person
        self.attach_id = "at_" + secrets.token_hex(8)
        self.seen = now
        self.cv = threading.Condition()
        self.q: collections.deque[dict[str, Any]] = collections.deque()
        self.wake: Any = None  # an async caller's ``loop.call_soon_threadsafe(event.set)``; one waiter at a time
        self.ended = False

    def push(self, msg: dict[str, Any]) -> bool:
        """Queue one message. ``False``: the queue is full (the caller closes the stream ``remote.busy``)."""
        with self.cv:
            if self.ended:
                return False
            if len(self.q) >= QUEUE_MAX and msg["t"] == "frame":
                return False
            self.q.append(msg)
            self.cv.notify_all()
            wake = self.wake
        if wake is not None:
            wake()
        return True

    def take(self, limit: int) -> list[dict[str, Any]]:
        with self.cv:
            out = []
            while self.q and len(out) < limit:
                out.append(self.q.popleft())
            return out

    def pending(self) -> bool:
        with self.cv:
            return bool(self.q)

    def wait(self, timeout: float) -> None:
        """Block (a thread) until something is queued or ``timeout`` seconds pass."""
        with self.cv:
            if not self.q and not self.ended:
                self.cv.wait(timeout)


def current(service: Any) -> Adapter | None:
    """The live adapter of the workspace, or ``None`` (none attached, or its lease ran out)."""
    with _MU:
        a: Adapter | None = getattr(service, _ATTR, None)
    if a is None or a.ended or service.clock() - a.seen > LEASE_S:
        return None
    return a


def attached(service: Any) -> bool:
    return current(service) is not None


def claim(service: Any, person: str, attach_id: str | None) -> Adapter:
    """``remote.attach``: the first call (``attach_id`` ``None``) takes the slot, a later one with the id refreshes the
    lease. Another live adapter: ``remote.busy``. An id that is not the live one (a lapsed lease): ``invalid.input``
    (the caller claims again). The person of the call must be the adapter's person."""
    now = service.clock()
    with _MU:
        a: Adapter | None = getattr(service, _ATTR, None)
        if a is not None and (a.ended or now - a.seen > LEASE_S):
            a.ended = True
            a = None
        if attach_id is None:
            if a is not None:
                raise HostError(
                    "remote.busy", "another dashboard is already attached to this workspace's remote access"
                )
            a = Adapter(person, now)
            setattr(service, _ATTR, a)
            return a
        if a is None or a.attach_id != attach_id or a.person != person:
            raise HostError("invalid.input", "remote.attach: the attachment is not live; attach again")
        a.seen = now
        return a


def release(service: Any, adapter: Adapter | None = None) -> bool:
    """End the attachment (``adapter`` must be the live one when given). Returns whether one was live."""
    with _MU:
        a: Adapter | None = getattr(service, _ATTR, None)
        if a is None or (adapter is not None and a is not adapter):
            return False
        setattr(service, _ATTR, None)
    with a.cv:
        a.ended = True
        a.cv.notify_all()
        wake = a.wake
    if wake is not None:
        wake()
    return True
