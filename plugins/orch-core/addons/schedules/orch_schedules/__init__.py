"""schedules: the switch for Mission Control's Schedules (docs/schedules.md), plus a Today tile.

The scheduler, the Schedules page and the findings on Today are orch-core's, because addons render widgets only and
cannot start processes; core runs them only while this addon is enabled in the workspace. Core writes a small status
file into this addon's state folder after every round; the tile reads it, nothing else.
"""
from __future__ import annotations

import json
from datetime import datetime

from orch.addons.widgets import Tile

STATUS = "status.json"


def read_status(state_dir) -> dict | None:
    """The runner's last status, or None when there is none (never run, or unreadable)."""
    try:
        data = json.loads((state_dir / STATUS).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _num(v) -> int | None:
    return v if isinstance(v, int) and not isinstance(v, bool) and v >= 0 else None


def sub_line(status: dict) -> str:
    parts = []
    findings = _num(status.get("findings"))
    if findings:
        parts.append(f"{findings} finding{'s' if findings != 1 else ''} for you")
    fix = _num(status.get("needs_fix"))
    if fix:
        parts.append(f"{fix} need{'s' if fix == 1 else ''} a look")
    nxt = status.get("next")
    if isinstance(nxt, str):
        try:
            parts.append("next " + datetime.fromisoformat(nxt).strftime("%H:%M"))
        except ValueError:
            pass
    return " · ".join(parts) or "armed"


class Schedules:
    def __init__(self, ctx):
        self.ctx = ctx

    def widgets(self, slot, view):
        if slot != "today.summary":
            return []
        status = read_status(view.state_dir)
        armed = _num((status or {}).get("armed"))
        if status is None or armed is None:
            return [Tile("Schedules", None, "neu", href="/schedules", sub="not run yet")]
        role = "warn" if _num(status.get("needs_fix")) else ("info" if armed else "neu")
        return [Tile("Schedules", armed, role, href="/schedules", sub=sub_line(status))]


def create(ctx):
    return Schedules(ctx)
