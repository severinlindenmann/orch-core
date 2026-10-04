"""Sprints (E2): planning metadata only, no gates. Defined per workspace in the config
(`sprints: [{id, name, start, end}]`, dates as YYYY-MM-DD); a ticket names one in its frontmatter `sprint`."""
from __future__ import annotations

from orch.clock import now
from orch.errors import ValidationError


def all_sprints(ws) -> list[dict]:
    out = []
    for s in ws.config.get("sprints") or []:
        if isinstance(s, dict) and isinstance(s.get("id"), (str, int)) and str(s["id"]).strip():
            out.append({"id": str(s["id"]), "name": str(s.get("name") or s["id"]),
                        "start": str(s.get("start") or ""), "end": str(s.get("end") or "")})
    return out


def find(ws, ref: str) -> dict:
    want = str(ref).strip().lower()
    for s in all_sprints(ws):
        if s["id"].lower() == want:
            return s
    known = ", ".join(s["id"] for s in all_sprints(ws)) or "none defined"
    raise ValidationError(f"no sprint {ref!r} in this workspace ({known})",
                          hint="sprints are defined under `sprints` in orchestrator/config.json")


def current(ws, today: str | None = None) -> dict | None:
    """The sprint whose start..end (inclusive) contains `today` (default: now, UTC)."""
    day = today or now().strftime("%Y-%m-%d")
    for s in all_sprints(ws):
        if s["start"] and s["end"] and s["start"] <= day <= s["end"]:
            return s
    return None
