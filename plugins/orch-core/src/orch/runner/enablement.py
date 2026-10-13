"""The machine switch "dashboard execution" (D-P2-31, owner decision 12 Oct 2026).

One switch per workspace on this account, turned on only by ``orch runner enable --workspace W`` in the person's own
terminal (``/dev/tty``, refused under ``ORCH_GRANT``). It gates everything the dashboard can make run as the person:
agent start, worktree git and terminals. ``--no-terminals`` hides terminals; it is a preference, **not** a safety
measure, because a started agent can already run any command.

The switch is not a boundary against a same-UID process: the dashboard has no op that turns it on, but a process of the
person's UID can write this file. It records when it was turned on and the exact text the person was shown.

File: ``<state>/host/dashboard-execution.json`` (0600, host UID), ``{"v":1, "workspaces":{"<ws>":{"enabled":true,
"terminals":bool, "at":"…", "text":"…"}}}``. A corrupt file reads as "off" everywhere (fail closed).
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from orch.host.custody import ensure_private_dir, read_json, write_private
from orch.host.errors import HostError
from orch.host.registry import host_dir

__all__ = ["CARD_TEXT", "NO_TERMINALS_TEXT", "flag_path", "read", "risk_text", "set_enabled", "state_of"]

#: the decided card line (tie-break D-P2-31): workspace card, Start dialog and terminal header
CARD_TEXT = (
    "Dashboard execution: agents and terminals started here run as you. Anything that takes over this dashboard, "
    "or any agent it starts, can run commands and read your files and keys."
)
NO_TERMINALS_TEXT = "--no-terminals only hides terminals in the dashboard; it does not make agent execution safe."


def risk_text(prefix: str, workspace_id: str, terminals: bool) -> str:
    lines = [
        f"Turn on dashboard execution for {prefix} ({workspace_id}) on this account.",
        "",
        CARD_TEXT,
        "",
        "That includes reading your SSH keys and your orch keys. The dashboard can then start agents, create "
        "worktrees" + (" and open terminals" if terminals else "") + " for this workspace, as you.",
        "Nothing on this machine that runs as you is kept out by this switch; it only decides what the dashboard is "
        "allowed to start.",
    ]
    if not terminals:
        lines += ["", NO_TERMINALS_TEXT]
    return "\n".join(lines) + "\n"


def flag_path(state_dir: Path) -> Path:
    return host_dir(state_dir) / "dashboard-execution.json"


def read(state_dir: Path, uid: int) -> dict[str, Any]:
    doc = read_json(flag_path(state_dir), uid)
    if doc is None:
        return {"v": 1, "workspaces": {}}
    if not isinstance(doc, dict) or doc.get("v") != 1 or not isinstance(doc.get("workspaces"), dict):
        raise HostError("internal", "the dashboard execution file is corrupt; dashboard execution stays off")
    return doc


def state_of(state_dir: Path, workspace_id: str, uid: int) -> dict[str, Any]:
    """``{enabled, terminals}`` for the workspace; any doubt is off."""
    try:
        entry = read(state_dir, uid)["workspaces"].get(workspace_id)
    except HostError:
        entry = None
    if not isinstance(entry, dict) or entry.get("enabled") is not True:
        return {"enabled": False, "terminals": False}
    return {"enabled": True, "terminals": entry.get("terminals") is True}


def set_enabled(
    state_dir: Path, workspace_id: str, on: bool, uid: int, *, terminals: bool = True, text: str = ""
) -> dict[str, Any]:
    ensure_private_dir(host_dir(state_dir), uid)
    doc = read(state_dir, uid)
    if on:
        stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        doc["workspaces"][workspace_id] = {"enabled": True, "terminals": terminals, "at": stamp, "text": text}
    else:
        doc["workspaces"].pop(workspace_id, None)
    write_private(flag_path(state_dir), doc)
    return doc
