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
from collections.abc import Callable
from pathlib import Path
from typing import Any

from orch.host.custody import ensure_private_dir, read_json, write_private
from orch.host.errors import HostError
from orch.host.registry import host_dir

__all__ = [
    "CARD_TEXT",
    "card_text",
    "NO_TERMINALS_TEXT",
    "TERMINALS_ON_TEXT",
    "flag_path",
    "read",
    "risk_text",
    "set_enabled",
    "set_terminals",
    "state_of",
    "wait_connected",
]

#: the decided card line (tie-break D-P2-31): workspace card, Start dialog and terminal header
CARD_TEXT = (
    "Agents and terminals you start here work as you on this computer. They can run programs and open your files, "
    "including secret keys, and so can anything that takes over this dashboard. Only continue on a computer you trust."
)
NO_TERMINALS_TEXT = "--no-terminals only hides terminals in the dashboard; it does not make agent execution safe."


def card_text(agent_user: str | None = None) -> str:
    """The card line: D-P2-31's where agents run as the person; tier B's (spec 13.5) when they run as ``agent_user``."""
    if agent_user is None:
        return CARD_TEXT
    return f"Dashboard execution: agents and terminals started here run as {agent_user}. It cannot read your keys."


def risk_text(prefix: str, workspace_id: str, terminals: bool, agent_user: str | None = None) -> str:
    """The text the person is shown (and the record keeps). With ``agent_user`` (tier B) the words say the agents run as
    that user; without it the text is exactly D-P2-31's, which the signing review also uses."""
    if agent_user is not None:
        opens = " and open terminals" if terminals else ""
        lines = [
            f"Turn on dashboard execution for {prefix} ({workspace_id}) on this account.",
            "",
            card_text(agent_user),
            "",
            f"The dashboard can then start agents, create worktrees{opens} for this workspace, as that user. "
            "It has no access to your SSH keys or your orch keys.",
            "Anything that takes over this dashboard can still start agents and give them work to do; it is not kept "
            "out by this switch.",
        ]
        if not terminals:
            lines += ["", NO_TERMINALS_TEXT]
        return "\n".join(lines) + "\n"
    lines = [
        f"Turn on dashboard execution for {prefix} ({workspace_id}) on this account.",
        "",
        CARD_TEXT,
        "",
        "Those keys include your SSH keys and your orch keys. The dashboard can then start agents, create "
        "worktrees" + (" and open terminals" if terminals else "") + " for this workspace, as you.",
        "Nothing on this machine that runs as you is kept out by this switch; it only decides what the dashboard is "
        "allowed to start.",
    ]
    if not terminals:
        lines += ["", NO_TERMINALS_TEXT]
    return "\n".join(lines) + "\n"


TERMINALS_ON_TEXT = (
    "Terminals: the dashboard may open shells and attach to runs as you. A shell is a program with your files, keys "
    "and network; anything that takes over the dashboard can type into it."
)


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


def set_terminals(state_dir: Path, workspace_id: str, on: bool, uid: int, *, text: str = "") -> dict[str, Any]:
    """``orch runner terminals on|off``: only for a workspace whose dashboard execution is already on. The time and
    the text shown are kept like the machine switch's. The host checks the switch at every attach and open, and its
    watcher ends live attachments within about a second once it is off (``runner.disabled``)."""
    doc = read(state_dir, uid)
    entry = doc["workspaces"].get(workspace_id)
    if not isinstance(entry, dict) or entry.get("enabled") is not True:
        raise HostError("runner.disabled", "dashboard execution is off for this workspace: orch runner enable first")
    entry["terminals"] = on
    entry["terminals_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    if on:
        entry["terminals_text"] = text
    write_private(flag_path(state_dir), doc)
    return doc


def wait_connected(
    probe: Callable[[], bool],
    timeout: float,
    *,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
    interval: float = 0.5,
) -> bool:
    """Whether the runner has said hello within ``timeout`` seconds: ``probe`` (``whoami.runner.connected`` on the
    person's workspace connection) is asked until it answers true. A probe that raises counts as "not yet". Used by
    ``orch setup`` before it signs a runner grant it could not deposit (B9 R4)."""
    deadline = clock() + timeout
    while True:
        try:
            if probe():
                return True
        except Exception:  # noqa: BLE001 - the host or the runner is still coming up
            pass
        if clock() >= deadline:
            return False
        sleep(interval)
