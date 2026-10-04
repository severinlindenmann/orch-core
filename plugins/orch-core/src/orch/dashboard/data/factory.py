"""AI Factory on the dashboard (#2, phase 2): what the permission cards, the epic's factory status and the Board
group show. Reads only; every answer goes through orch.core.permits (human only, signed). Off unless
`factory.enabled`: every function answers "nothing" then. Agent text (a command, a reason) is shown escaped."""
from __future__ import annotations

from datetime import timedelta

from orch import clock
from orch.core import epics, permits, store


def _raw(text) -> str:
    """Text for a card: only characters outside printable ASCII (and newlines) are escaped, so a grantable command
    (single line, printable ASCII) reads exactly as the raw text the grant binds, quotes and backslashes included."""
    return "".join(c if 32 <= ord(c) < 127 else c.encode("unicode_escape").decode("ascii") for c in str(text))


def _card(r: dict) -> dict:
    return {"id": r["id"], "epic": r["epic"], "ticket": r["ticket"], "sha": r["sha"], "short": r["sha"][7:15],
            "command": _raw(r["command"]), "reason": _raw(r["reason"]),
            "asked_by": _raw(r["actor"]), "source": _raw(r["source"]), "at": r["at"]}


def permit_view(ws, epic_id: str | None = None) -> dict | None:
    """{requests: open permission cards, grants: live standing grants for an epic, budget: used-up-budget cards}, all
    for `epic_id` when given. None while the factory is off."""
    if not permits.enabled(ws):
        return None
    keep = (lambda e: e.upper() == epic_id.upper()) if epic_id else (lambda e: True)
    reqs = [_card(r) for r in permits.open_requests(ws) if keep(str(r["epic"]))]
    grants = [{"grant": g["grant"], "epic": g["epic"], "scope": g["scope"], "command": _raw(g["command"])}
              for g in permits.grants(ws) if g["live"] and keep(str(g["epic"]))]
    cards = [c for c in permits.budget_cards(ws) if keep(str(c["epic"]))]
    return {"requests": reqs, "grants": grants, "budget": cards,
            "any": bool(reqs or grants or cards)}


def epic_status(ws, epic, d: dict | None, events) -> dict | None:
    """The factory chip of an epic page: {enabled, factory, state, children, max_children, hours_left, max_hours}.
    `factory` is whether the signed charter is a factory one; None-free only while the switch is on."""
    if not permits.enabled(ws):
        return None
    limits = {"max_children": epics.FACTORY_DEFAULTS["max_children"], "max_size": epics.FACTORY_DEFAULTS["max_size"],
              "max_hours": epics.FACTORY_DEFAULTS["max_hours"]}
    if not d or not d.get("factory"):
        return {"factory": False, "limits": limits}
    used = epics.delegated_count(ws, epic.id, d["id"], events)
    left = None
    try:
        end = clock.parse_stamp(str(d["at"])) + timedelta(hours=d["max_hours"])
        left = max(0.0, (end - clock.now()).total_seconds() / 3600)
    except (ValueError, TypeError):
        left = 0.0  # no readable time: the budget counts as used up
    if d["paused"]:
        state = "paused"
    elif d["epic_changed"]:
        state = "suspended"
    elif d.get("expired") or used >= d["max_children"]:
        state = "budget used up"
    else:
        state = "running"
    return {"factory": True, "limits": limits, "state": state, "children": used, "max_children": d["max_children"],
            "hours_left": left, "max_hours": d["max_hours"]}


def factory_epic_ids(ws, entries) -> set[str]:
    """Upper-case ids of the epics whose signed charter is a factory one (the Board's factory group); empty off."""
    if not permits.enabled(ws):
        return set()
    from orch.core import ledger
    signed, out = ledger.entries(ws), set()
    for e in entries:
        if e.meta is None or not epics.is_epic(e.meta):
            continue
        try:
            if permits.factory_delegation(ws, store.read_ticket(e.path), signed):
                out.add(e.id.upper())
        except Exception:
            continue
    return out
