"""Dashboard state per workspace that is not ticket data (E2: the Board/List "Group by"; C: whether the Board's
Backlog lane is unfolded). Kept in the user's orch
config dir (`dashboard.json`, keyed by the ledger's workspace id), never in the repository. Best effort: a missing or
broken file means the defaults, and a failed write changes nothing but the memory."""
from __future__ import annotations

import json

from orch.core.fsutil import atomic_write_text

GROUPS = ("none", "epic", "sprint", "label", "agent", "repo", "factory")
FILE = "dashboard.json"


def prefs_path():
    from orch.dashboard.launch import config_dir
    return config_dir() / FILE


def _load() -> dict:
    try:
        data = json.loads(prefs_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _key(ws) -> str:
    from orch.core.ledger import workspace_id
    return workspace_id(ws)


def group_by(ws) -> str:
    entry = _load().get(_key(ws))
    value = entry.get("group_by") if isinstance(entry, dict) else None
    return value if value in GROUPS else "none"


def set_group_by(ws, value: str) -> None:
    if value not in GROUPS:
        return
    _set(ws, "group_by", value)


def backlog_open(ws) -> bool:
    """C: the Board's Backlog lane starts folded to its count; unfolded only once the user chose it."""
    entry = _load().get(_key(ws))
    return isinstance(entry, dict) and entry.get("backlog_open") is True


def set_backlog_open(ws, value: bool) -> None:
    _set(ws, "backlog_open", bool(value))


def _set(ws, name: str, value) -> None:
    data = _load()
    entry = data.get(_key(ws)) if isinstance(data.get(_key(ws)), dict) else {}
    if entry.get(name) == value:
        return
    data[_key(ws)] = {**entry, name: value}
    try:
        prefs_path().parent.mkdir(parents=True, exist_ok=True)
        atomic_write_text(prefs_path(), json.dumps(data, indent=2) + "\n")
    except OSError:
        pass
