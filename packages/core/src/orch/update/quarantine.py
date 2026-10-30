"""The QUARANTINE flag: how ``orch update`` starts the new code for its health check (spec, "Data across updates").

While the marker ``<state>/install/quarantine`` exists, a host that starts:

* serves ONLY its control socket, and there only the read ``host.list`` (so "the host answers" can be checked); every
  other control op is refused with ``host.unavailable``;
* does not start the runner socket, the webhook receiver, the bus or any workspace service. So no agent can connect,
  no hub link is opened or dialled, and nothing is signed, appended or recovered (intent recovery is skipped too).

It is a file, not an environment variable, because launchd and systemd start the host with their own environment.
The marker only ever makes things MORE closed, and a host that reads it fails closed (a broken symlink counts).
``orch update`` writes it before the quarantined start and removes it before the normal start, on every path
(commit, rollback, resume). A person can also remove it by hand with ``orch update --rollback`` (it is cleared there).
:func:`orch.migrate.format.open_root` lets a quarantined process pass the "unfinished update" refusal and nothing else.
"""

from __future__ import annotations

import os
from pathlib import Path

__all__ = ["activate", "active", "clear", "marker"]


def marker(state: Path) -> Path:
    return Path(state) / "install" / "quarantine"


def active(state: Path) -> bool:
    return os.path.lexists(marker(state))


def activate(state: Path) -> None:
    p = marker(state)
    p.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    os.close(fd)


def clear(state: Path) -> None:
    try:
        marker(state).unlink()
    except FileNotFoundError:
        pass
