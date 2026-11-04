"""The host's network settings for hub links (R6 #1594, design section 3), and what must outlive one transport.

* :func:`load` reads the network settings kept under ``<state>/hosts`` (:mod:`orch.hubclient.netconfig`). Every
  place that dials a hub passes the result as ``net=``; a file that cannot be used raises ``ProxyRefused`` (an
  ``OSError``: the hub is unreachable, never a silent direct dial). The environment is never read.
* :func:`inspected` hands out the no-dial block after an inspected mismatch, one per (state dir, hub address): the
  five minutes survive a transport being re-created (a new ``TlsTransport`` per call, a redial of the stream link).
* :func:`set_proxy` / :func:`clear_proxy` / :func:`view` are the owner's settings (the route in ``hubops``); the
  password is written to its 0600 file and never returned.
"""

from __future__ import annotations

import os
import threading
from pathlib import Path
from typing import Any

from orch.hubclient.netconfig import NetConfigError, NetSettings, clear_net, load_net, write_net
from orch.hubclient.tlsadapter import InspectedBlock
from orch.hubclient.tlslink.errors import ProxyRefused

__all__ = ["clear_proxy", "forget_blocks", "inspected", "load", "net_dir", "set_proxy", "view"]

_MU = threading.Lock()
_BLOCKS: dict[tuple[str, str], InspectedBlock] = {}


def net_dir(state_dir: Path | str) -> Path:
    return Path(os.path.realpath(state_dir)) / "hosts"


def load(state_dir: Path | str) -> NetSettings:
    try:
        return load_net(net_dir(state_dir))
    except NetConfigError:
        raise ProxyRefused("The network settings of this computer cannot be used. Nothing was dialled.") from None


def inspected(state_dir: Path | str, addr: str) -> InspectedBlock:
    """The shared block of one hub address (in memory; a restart of the host forgets it, which is fine)."""
    with _MU:
        return _BLOCKS.setdefault((str(state_dir), addr), InspectedBlock())


def forget_blocks() -> None:
    """Tests, and a change of the proxy (the new route deserves a try now)."""
    with _MU:
        _BLOCKS.clear()


def view(state_dir: Path | str) -> dict[str, Any]:
    """What the owner may see: the proxy address, the user name, whether a password is kept. Never the password."""
    d = net_dir(state_dir)
    try:
        net = load_net(d)
    except NetConfigError:
        return {"proxy": None, "user": None, "has_password": False, "broken": True}
    p = net.proxy
    if p is None:
        return {"proxy": None, "user": None, "has_password": False, "broken": False}
    return {
        "proxy": f"{p.scheme}://{('[' + p.host + ']') if ':' in p.host else p.host}:{p.port}",
        "user": p.user,
        "has_password": p.password is not None,
        "broken": False,
    }


def set_proxy(state_dir: Path | str, proxy: str, user: str | None, password: str | None) -> None:
    """``NetConfigError`` if the proxy, user or password is not acceptable; nothing is changed then."""
    write_net(net_dir(state_dir), proxy, user, password)
    forget_blocks()  # the new route deserves a try now


def clear_proxy(state_dir: Path | str) -> None:
    clear_net(net_dir(state_dir))
    forget_blocks()
