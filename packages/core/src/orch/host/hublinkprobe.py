"""D78: is the hub reachable right now? A plain TCP connect to the hub address, bounded and cached.

The probe never dials a hub *link*: the hub serves one live link per workspace and a second one replaces the
first (protocol 10.7 rule 6). It only answers "does the port take a connection", so ``hub.status`` can say
``unreachable`` instead of repeating the stored binding state. A pin or authentication failure on the real link
shows as ``link_error`` (the peer runtime's last error). Nothing here reads or sends a secret, and nothing here
raises into ``hub.status``."""

from __future__ import annotations

import socket
import threading
import time
from typing import Any

from orch.wsproto.localdial import dial_host

PROBE_TIMEOUT_S = 1.0
CACHE_S = 3.0

_MU = threading.Lock()
_CACHE: dict[tuple[str, str], tuple[float, bool]] = {}


def reachable(scope: str, addr: str | None, *, now: float | None = None) -> bool | None:
    """``True`` / ``False`` for the hub address, ``None`` when it is unusable. Cached ``CACHE_S`` seconds."""
    if not addr:
        return None
    from orch.hubclient.errors import HubRefused
    from orch.hubclient.tlsadapter import parse_addr

    try:
        host, port = parse_addr(addr)
    except HubRefused:
        return None
    t = time.monotonic() if now is None else now
    key = (scope, addr)
    with _MU:
        hit = _CACHE.get(key)
        if hit is not None and 0 <= t - hit[0] < CACHE_S:
            return hit[1]
    try:
        with socket.create_connection((dial_host(host), port), timeout=PROBE_TIMEOUT_S):
            ok = True
    except (OSError, ValueError):
        ok = False
    with _MU:
        _CACHE[key] = (t, ok)
    return ok


def clear() -> None:
    """Forget the cache (tests)."""
    with _MU:
        _CACHE.clear()


def link_view(binding: str | None, up: bool | None) -> str | None:
    """The ``link`` field of ``hub.status``: the binding state, except a bound link whose hub does not answer."""
    if binding == "bound" and up is False:
        return "unreachable"
    return binding


def peer_view(service: Any) -> dict[str, Any]:
    """``peer_outbox`` (envelopes the hub has not taken yet) and ``link_error`` (the receive loop's last failure)."""
    from . import peerops

    return peerops.status_fields(service)
