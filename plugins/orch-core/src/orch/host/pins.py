"""Root-owned agent and worker pins (D-P2-6): read-only here. The setup helper (H3c) writes them.

A pin is ``<dir>/<uid>.json``, a regular file owned by root and writable by nobody else, holding
``{"class": "agent"|"worker", "host_uid": int, "workspaces": [WsId, ...]}``. H3c owns the format; H2a reads only
what it needs: whether a UID is pinned at all (such a UID can never be mapped as ``person``, §0.8.3) and which
workspaces an agent UID's pin names (those are registered for its direct-mode CLI, D-P2-18). A file that is not
root-owned is no pin and is ignored.
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


class AgentPins:
    def __init__(self, directory: Path | None = None, *, owner: int = 0) -> None:
        self.directory = DEFAULT_PIN_DIR if directory is None else Path(directory)
        self.owner = owner  # tests use their own UID; production pins are root-owned

    def get(self, uid: int) -> dict[str, Any] | None:
        path = self.directory / f"{int(uid)}.json"
        try:
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
        except OSError:
            return None
        with os.fdopen(fd, "rb") as handle:
            info = os.fstat(handle.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_uid != self.owner or stat.S_IMODE(info.st_mode) & 0o022:
                return None
            raw = handle.read(65536)
        try:
            doc = json.loads(raw)
        except ValueError:
            doc = None
        # A trusted but unreadable pin still pins the UID: never let a broken pin turn an agent into a person.
        return doc if isinstance(doc, dict) else {"class": "agent", "workspaces": []}

    def pinned(self, uid: int) -> bool:
        return self.get(uid) is not None  # any trusted pin, whatever its class

    def workspaces(self, uid: int) -> set[str]:
        doc = self.get(uid)
        items = doc.get("workspaces") if doc else None
        return {w for w in items if isinstance(w, str)} if isinstance(items, list) else set()
