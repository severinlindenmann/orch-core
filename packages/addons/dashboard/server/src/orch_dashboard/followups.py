"""Follow-ups (Schedules add-on, #720): what the agents promised to come back to, and what went wrong with it.

Read only. The host's own `follow_up.list` (a registry read, served on the workspace socket under the viewer's
visibility) is the only source; this module adds the ticket title and what the viewer may do, and withholds hidden text.
It writes nothing: Cancel is a signed hand-off (`handoff.py`, action `followup_cancel`), Run now is the normal
Start agent flow, Hide is a per-person choice the page keeps. What the host cannot do yet (Reschedule, Undo of a Cancel)
is said in `can`, so the page never offers it.
"""

from __future__ import annotations

from fastapi import Depends, FastAPI

from . import adapters, reads
from .adapters import board_statuses
from .adapters.addons import display
from .errors import HostError, fail
from .gateway import Gateway
from .text import hidden

ADDON = "schedules"
#: the states a row can have (the host's `view_state`); anything else is dropped, never shown
STATES = ("pending", "needs_person", "fired", "ran", "failed", "did_not_run", "cancelled")
TRIGGERS = ("time", "orch", "addon", "webhook", "deadline")
#: the closed reason lists of the spec (§6); a reason outside them is shown as none (the page says "no reason given")
REASONS = frozenset(
    {
        "no_host", "harness_failed", "budget", "runner_unavailable", "runner_restarted", "host_restarted",
        "grant_revoked", "ticket_moved_on", "no_writes", "error",
        "replaced", "ticket_left_in_progress", "claimed_by_other", "member_removed", "addon_disabled", "chain_cap",
        "person", "agent", "run_now", "dismissed",
        "no_event", "no_ring", "addon_silent",
    }
)  # fmt: skip
MANAGERS = ("owner", "maintainer")


def note(value) -> str | None:
    """The agent brief: newlines and tabs are normal; any other invisible character withholds it."""
    return value if isinstance(value, str) and not hidden(value.replace("\n", "").replace("\t", "")) else None


def row(r: dict, titles: dict, who: str, role: str) -> dict | None:
    """One follow-up as the page shows it, or None for a record that is not one this page can draw."""
    if not isinstance(r, dict) or r.get("state") not in STATES or not isinstance(r.get("id"), str):
        return None
    key = r.get("key") if isinstance(r.get("key"), str) else None
    reason = r.get("reason")
    mine = r.get("person") == who
    live = r["state"] in ("pending", "needs_person")
    return {
        "id": r["id"],
        "ticket": key,
        "title": titles.get(key),
        "state": r["state"],
        "trigger": r.get("trigger") if r.get("trigger") in TRIGGERS else "time",
        "due": r.get("due"),
        "deadline": r.get("deadline"),
        "set_at": r.get("set_at"),
        "fired_at": r.get("fired_at"),
        "late_by": r["late_by"] if isinstance(r.get("late_by"), int) and r["late_by"] > 0 else None,
        "brief": note(r.get("brief")),
        "brief_hidden": isinstance(r.get("brief"), str) and note(r.get("brief")) is None,
        "outcome": r.get("outcome") if r.get("outcome") in ("ran", "failed", "did_not_run") else None,
        "reason": reason if reason in REASONS else None,
        "running": r["state"] == "fired",
        "person": r.get("person") if isinstance(r.get("person"), str) else None,
        "mine": mine,
        "label": display(r.get("label")),
        # the host decides who may cancel; the page only hides a button it knows is refused
        "can_cancel": live and (mine or role in MANAGERS),
    }


def register(app: FastAPI, gateway: Gateway) -> None:
    def titles(ws) -> dict:
        try:
            rows, _ = reads.all_rows(gateway, ws, {"status": board_statuses(), "sort": "updated"}, cap=400)
        except HostError:
            return {}
        return {r["key"]: display(r.get("title")) for r in rows if isinstance(r.get("key"), str)}

    @app.get("/api/w/{ws}/followups")
    def followups(ws: str = Depends(reads.workspace)):
        link = gateway.enabled(ws)
        if not link.supports("follow_up.list"):
            return {"available": False}
        if not gateway.addon_on(link, ADDON):
            raise fail("addon.disabled", exit=3)  # Schedules is off: nothing about follow-ups, whatever the page asks
        who = link.welcome["peer"]["person"]
        role = next((m.role for m in adapters.members(link.call("member.list")) if m.person == who), "viewer")
        data = link.call("follow_up.list", {"all": True})
        names = titles(ws)
        rows = [x for x in (row(r, names, who, role) for r in data.get("rows") or []) if x]
        return {
            "available": True,
            "role": role,
            # What this host can do. A page never offers what is false here.
            "can": {"cancel": role != "viewer", "reschedule": False, "undo": False},
            "rows": rows,
        }
