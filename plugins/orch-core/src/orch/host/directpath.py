"""Is the workspace's direct relay path open? (V3 C10, design-c10 M2/M3, hub protocol 10.8.3)

The switch lives in ``hub.json`` (``HubState.direct``): ``open`` (also: not set), ``closing`` (the host has started the
cut-over: from here it behaves as closed, fail closed) and ``closed`` (the relay confirmed ``cut_over``). This module
only **reads** it, for the places that must not depend on the store lock or a hub library:

* :func:`direct_state`: ``open`` | ``closing`` | ``closed``. A ``hub.json`` that exists but cannot be read, or says
  something else, is ``closed`` (fail closed: the host refuses to guess, as ``hub.state_mismatch`` does);
* :func:`closed`: ``closing`` or ``closed``;
* :func:`allows`: the allow-list of the one gate every relay connection of a workspace passes
  (:class:`orch.net.http.RelayConn` ``allowed=``): with the path not open, exactly the workspace's cut-over routes and
  the session mechanics they need (login, the holder generation the fenced writes name, relay-info). Every other route
  raises ``cut_over`` before a byte is sent. There is no bypass flag.
* :func:`gate`: the callable a ``RelayConn`` takes, bound to one workspace (cached by the file's identity, so a relay
  thread polling every second costs one ``stat``).
"""

from __future__ import annotations

import os
import threading
from collections.abc import Callable
from pathlib import Path

from .errors import HostError

__all__ = ["OPEN_STATES", "allows", "closed", "direct_state", "gate", "hub_json"]

OPEN_STATES = ("open", "closing", "closed")
_SESSION = frozenset({"/v2/session/challenge", "/v2/session/login", "/v2/session/logout", "/v2/relay-info"})


def hub_json(state_dir: str | os.PathLike[str], workspace_id: str) -> Path:
    return Path(os.path.realpath(state_dir)) / "hosts" / workspace_id / "hub" / "hub.json"


def direct_state(state_dir: str | os.PathLike[str], workspace_id: str, host_uid: int) -> str:
    """``open`` | ``closing`` | ``closed`` as ``hub.json`` says; ``open`` when there is no ``hub.json`` or it does not
    set ``direct``; ``closed`` when it exists and cannot be read (fail closed)."""
    from .custody import read_json

    path = hub_json(state_dir, workspace_id)
    if not os.path.lexists(path):
        return "open"
    try:
        doc = read_json(path, host_uid, limit=1 << 20)
    except (HostError, OSError):
        return "closed"
    if not isinstance(doc, dict):
        return "closed"
    d = doc.get("direct")
    if d is None:
        return "open"
    return d if d in OPEN_STATES else "closed"


def closed(state_dir: str | os.PathLike[str], workspace_id: str, host_uid: int) -> bool:
    return direct_state(state_dir, workspace_id, host_uid) != "open"


def allows(state: str, workspace_id: str, method: str, target: str) -> bool:
    """May ``method target`` go to the relay while the direct path is ``state``? ``open``: yes (the relay's own rules
    apply). Otherwise only the workspace's cut-over routes (``GET``, ``PUT``, ``DELETE``: the cut-over, its resume and
    the reversal), plus the session mechanics those fenced writes need: ``POST /v2/session/{challenge,login,logout}``,
    ``GET /v2/relay-info`` and the holder generation (``POST /v2/workspaces/{ws}/host/hold``). Nothing else: not the
    bridge, poll, card, genesis, members, checkpoint, staging, push or any route added later."""
    if state == "open":
        return True
    path = target.split("?", 1)[0]
    base = f"/v2/workspaces/{workspace_id}"
    if path == base + "/cutover":
        return method in ("GET", "PUT", "DELETE")
    if path == base + "/host/hold":
        return method == "POST"
    if path in _SESSION:
        return (method == "GET") if path == "/v2/relay-info" else (method == "POST")
    return False


def gate(state_dir: str | os.PathLike[str], workspace_id: str, host_uid: int) -> Callable[[str, str], bool]:
    """The ``RelayConn(allowed=...)`` of one workspace: reads the switch fresh (cached on the file's identity)."""
    path = hub_json(state_dir, workspace_id)
    lock = threading.Lock()
    cache: dict[str, object] = {"key": object(), "state": "closed"}

    def allowed(method: str, target: str) -> bool:
        try:
            st = os.stat(path)
            key: object = (st.st_ino, st.st_size, st.st_mtime_ns)
        except FileNotFoundError:
            key = None
        except OSError:
            key = "error"
        with lock:
            if cache["key"] != key or key == "error":
                cache["state"] = direct_state(state_dir, workspace_id, host_uid)
                cache["key"] = key
            state = str(cache["state"])
        return allows(state, workspace_id, method, target)

    return allowed
