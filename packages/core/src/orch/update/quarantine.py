"""The QUARANTINE flag: how ``orch update`` starts the new code for its health check (spec, "Data across updates").

While the marker ``<state>/install/quarantine`` exists, a host that starts:

* serves ONLY its control socket, and there only the read ``host.list`` (so "the host answers" can be checked); every
  other control op is refused with ``host.unavailable``;
* does not start the runner socket, the webhook receiver, the bus or any workspace service. So no agent can connect,
  no hub link is opened or dialled, and nothing is signed, appended or recovered (intent recovery is skipped too).

It is a file, not an environment variable, because launchd and systemd start the host with their own environment.
The marker only ever makes things MORE closed, and a host that reads it fails closed (a broken symlink counts).
``orch update`` writes it before the quarantined start and removes it before the normal start, on every path
(commit, rollback, resume).

ONLY the host can run quarantined. hub, relay, publish and the dashboard are started after the commit.
:func:`orch.migrate.format.open_root` (``quarantine_ok=True``, passed only by the host) lets a process through an
unfinished update journal exactly while this marker exists; every other process is refused by that journal.
"""

from __future__ import annotations

import os
from pathlib import Path

__all__ = ["QUARANTINE_KEYS", "activate", "active", "clear", "marker"]

#: the only service that runs before the commit point; hub, relay, publish and the dashboard start after it
QUARANTINE_KEYS = ("host",)


def marker(state: Path) -> Path:
    return Path(state) / "install" / "quarantine"


def active(state: Path) -> bool:
    return os.path.lexists(marker(state))


def activate(state: Path, update_id: str) -> None:
    """Write the marker: a regular file (``O_EXCL|O_NOFOLLOW``, 0600) whose content is the ``update_id`` of the
    unfinished update journal. :func:`orch.migrate.format.open_root` admits a quarantined process only when the content
    matches the journal (and the journal is not ``restoring``), so a stale or planted marker admits nothing."""
    if not update_id or not all(c in "0123456789abcdef" for c in update_id):
        raise ValueError("the update id must be hex")
    p = marker(state)
    p.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    clear(state)
    fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        os.write(fd, (update_id + "\n").encode())
    finally:
        os.close(fd)


def clear(state: Path) -> None:
    try:
        marker(state).unlink()
    except FileNotFoundError:
        pass
