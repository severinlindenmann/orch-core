"""Root-owned agent and worker pins (D-P2-6): read-only here. The setup helper (H3c) writes them.

A pin is ``<dir>/<uid>.json``, a regular file owned by root and writable by nobody else, holding
``{"class": "agent"|"worker", "host_uid": int, "workspaces": [WsId, ...]}``. H3c owns the format; H2a reads only
what it needs: whether a UID is pinned at all (such a UID can never be mapped as ``person``, §0.8.3) and which
workspaces an agent UID's pin names (those are registered for its direct-mode CLI, D-P2-18). A file that is not
root-owned, a directory others can write, or any read error other than "not found" counts as a pin for every
workspace: an unreadable pin never turns an agent into a person.
"""

from __future__ import annotations

import json
import os
import stat
import sys
from pathlib import Path
from typing import Any

__all__ = ["DEFAULT_PIN_DIR", "AgentPins"]

DEFAULT_PIN_DIR = Path("/Library/Application Support/orch/pins") if sys.platform == "darwin" else Path("/etc/orch/pins")


_BROKEN: dict[str, Any] = {"class": "agent", "workspaces": "*"}


class AgentPins:
    """Fail closed: anything but a missing directory or a missing pin file reads as "pinned for every workspace"."""

    def __init__(self, directory: Path | None = None, *, owner: int = 0) -> None:
        self.directory = DEFAULT_PIN_DIR if directory is None else Path(directory)
        self.owner = owner  # tests use their own UID; production pins are root-owned

    def get(self, uid: int) -> dict[str, Any] | None:
        from .custody import check_tree
        from .errors import HostError

        try:  # the directory and every ancestor: owned by root (or the trusted owner), writable by nobody else
            check_tree(self.directory, self.owner)
        except HostError as exc:
            return None if exc.code == "not_found" else _BROKEN
        except OSError:
            return _BROKEN
        path = self.directory / f"{int(uid)}.json"
        try:
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
        except FileNotFoundError:
            return None
        except OSError:
            return _BROKEN  # EACCES, ELOOP, EIO: never "no pin"
        with os.fdopen(fd, "rb") as handle:
            info = os.fstat(handle.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_uid != self.owner or stat.S_IMODE(info.st_mode) & 0o022:
                return _BROKEN  # only the owner writes here; anything else is tampering
            raw = handle.read(65536)
        try:
            doc = json.loads(raw)
        except ValueError:
            doc = None
        return doc if isinstance(doc, dict) else _BROKEN

    def pinned(self, uid: int) -> bool:
        return self.get(uid) is not None  # any trusted (or unreadable) pin, whatever its class

    def names_workspace(self, uid: int, workspace_id: str) -> bool:
        doc = self.get(uid)
        if doc is None:
            return False
        items = doc.get("workspaces")
        return items == "*" or (isinstance(items, list) and workspace_id in items) or not isinstance(items, list)
