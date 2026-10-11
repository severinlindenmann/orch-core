"""Addon "needs you" entries: the rules of every active addon, evaluated on verified ticket state (§8.1).

The adapter between the replayed views (``WorkspaceView``, ``TicketView``) and the pure language of
:mod:`orch.addons.needs_rules`. An entry changes nothing: no gate, no status, no event. A rule of an inactive addon is
never evaluated and never reads inactive data.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from orch.addons.needs_rules import GATES, evaluate
from orch.addons.registry import Registry
from orch.model import Need
from orch.model.views import TicketView, WorkspaceView

__all__ = ["environment", "needs_of_ticket", "tokens"]


def _plain(v: Any) -> Any:
    if isinstance(v, Mapping):
        return {k: _plain(x) for k, x in v.items()}
    if isinstance(v, tuple | list):
        return [_plain(x) for x in v]
    return v


def environment(t: TicketView) -> dict[str, Any]:
    """The inputs a rule can read (the closed list of §8.1) for one ticket."""
    f = t.fields
    return {
        "status": t.status,
        "type": t.type,
        "size": f["size"],
        "priority": f["priority"],
        "labels": list(f["labels"]),
        "open_questions": sum(1 for q in t.questions if not q.answered),
        "tasks_open": sum(1 for x in t.tasks if x.state in ("open", "started", "blocked")),
        "acceptance": len(t.acceptance),
        "blocked": bool(f["blocked_by"]),
        "gates": {g: bool(t.gates[g].approved) for g in GATES},
    }


def tokens(ws: WorkspaceView, t: TicketView, person: str) -> set[str]:
    """The §5.9 tokens a person holds on the ticket; a viewer holds none."""
    m = ws.members.get(person)
    if m is None or m.role == "viewer":
        return set()
    out = {m.role}
    if t.owner == person:
        out.add("ticket_owner")
    out |= {role for role in ("assignees", "reviewers", "watchers") if person in t.people.get(role, ())}
    return out


def _can_see(ws: WorkspaceView, t: TicketView, person: str) -> bool:
    v = t.visibility
    return person in ws.members and (v == "workspace" or person in v["restricted"])


def needs_of_ticket(registry: Registry, ws: WorkspaceView, t: TicketView) -> list[Need]:
    out: list[Need] = []
    env = environment(t)
    for addon, rule in registry.needs_rules():
        if not evaluate(rule["when"], env, _plain(t.fields["addons"].get(addon, {}))):
            continue
        want = set(rule["who"])
        who = tuple(sorted(p for p in ws.members if want & tokens(ws, t, p) and _can_see(ws, t, p)))
        if who:
            out.append(Need("addon", t.uid, who, f"{addon}.{rule['id']}", rule["text"]))
    return out
