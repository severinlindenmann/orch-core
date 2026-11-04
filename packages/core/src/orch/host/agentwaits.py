"""Run-10 J04: "the agent waits in ``orch wait``" for the dashboard's one idle signal.

An agent blocked in ``orch wait`` keeps its terminal busy (the harness prints a ticking timer), so the terminal-quiet
signal never fires. The host serves that wait itself (``dispatch.wait``), so it knows: the coroutine records an entry
when its first look parks and removes it in its ``finally``. Memory only: never on disk, never logged, never signed,
never sent to a hub; display only, it decides nothing (an approval still needs the person's signature).

Stale entries fail closed to "not waiting": a killed CLI closes its connection and the transport cancels the request
(the ``finally`` runs); a host restart forgets everything; the reader ignores an entry past its wait's deadline; and
:func:`orch.host.runners.waiting_of` looks only at live runs. An entry matches a run by the host-verified grant id
(never a label the agent chooses) and the ticket key.
"""

from __future__ import annotations

import itertools
import threading
import time
from typing import Any

#: seconds an entry outlives its wait's own deadline before a reader ignores it
GRACE = 5.0

_lock = threading.Lock()
_ids = itertools.count(1)


def _table(service: Any) -> dict[int, dict[str, Any]]:
    got = getattr(service, "agent_waits", None)
    if got is None:
        got = service.agent_waits = {}
    return got


def enter(service: Any, grant_id: str | None, key: str | None, timeout: float, now: float | None = None) -> int | None:
    """Record one parked wait of ``grant_id`` on ticket ``key``; the token for :func:`leave`, or ``None`` (no grant:
    not an agent's wait)."""
    if not grant_id or not key:
        return None
    at = time.time() if now is None else now
    with _lock:
        token = next(_ids)
        _table(service)[token] = {"grant": grant_id, "key": key, "since": at, "until": at + timeout + GRACE}
    return token


def leave(service: Any, token: int | None) -> None:
    if token is None:
        return
    with _lock:
        _table(service).pop(token, None)


def of(service: Any, rec: dict[str, Any], now: float | None = None) -> float | None:
    """When the agent of run ``rec`` began to wait in ``orch wait`` on its ticket (oldest live entry), else ``None``."""
    grant, key = rec.get("grant"), rec.get("ticket")
    if not grant or not key:
        return None
    at = time.time() if now is None else now
    with _lock:
        rows = list(_table(service).values())
    since = [r["since"] for r in rows if r["grant"] == grant and r["key"] == key and r["until"] > at]
    return min(since) if since else None
