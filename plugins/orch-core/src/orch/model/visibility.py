"""Visibility (T12; ticket-format §9): ``workspace`` or ``{restricted: [persons]}``. The host enforces it, so the
model refuses what an invisible person or an agent of one would write."""

from __future__ import annotations

from typing import Any

from . import claims
from .codes import Code, Refusal
from .types import TCore, WsCore

# Events that manage a ticket and so work for owners and maintainers who are not on the list.
MANAGEMENT = frozenset(
    {
        "visibility.changed",
        "ticket.closed",
        "ticket.reopened",
        "people.changed",
        "policy.changed",
        "restore",
        "invalid.acknowledged",
    }
)


def can_see(ws: WsCore, t: TCore, person: str) -> bool:
    if person not in ws.members:
        return False
    v = t.fields["visibility"]
    return v == "workspace" or person in v["restricted"]


def is_restricted(t: TCore) -> bool:
    return t.fields["visibility"] != "workspace"


def changed(ws: WsCore, t: TCore, e: dict[str, Any]) -> Refusal | None:
    if not claims.may_manage(ws, t, e["actor"]["id"]):
        return Refusal(Code.ROLE_DENIED, "only the ticket owner, owners and maintainers change visibility")
    v = e["visibility"]
    if v != "workspace":
        unknown = [p for p in v["restricted"] if p not in ws.members]
        if unknown:
            return Refusal(Code.MEMBER_UNKNOWN, f"not members: {unknown}")
    t.fields["visibility"] = v
    return None
