"""Visibility (T12; ticket-format §9): ``workspace`` or ``{restricted: [persons]}``. The host enforces it, so the
model refuses what an invisible person or an agent of one would write."""

from __future__ import annotations

from typing import Any

from . import claims, identity
from .codes import Code, Refusal
from .types import TCore, WsCore

# Events that manage a ticket and so work for owners and maintainers who are not on the list.
MANAGEMENT = frozenset(
    {
        "visibility.changed",
        "ticket.moved",
        "ticket.closed",
        "ticket.reopened",
        "people.changed",
        "policy.changed",
        "restore",
        "invalid.acknowledged",
    }
)


def can_see(ws: WsCore, t: TCore, person: str) -> bool:
    """A person with a role now (a member, or a hub user with a pinned role) sees a workspace ticket; a restricted one
    only when listed. Both sides are canonical, so ``u_x`` aliased to ``p_x`` and ``p_x`` are one person (D59)."""
    person = identity.canonical(ws, person)
    if person not in ws.members and not (person.startswith("u_") and identity.known(ws, person)):
        return False
    v = t.fields["visibility"]
    return v == "workspace" or person in identity.canonical_set(ws, v["restricted"])


def is_restricted(t: TCore) -> bool:
    return t.fields["visibility"] != "workspace"


def changed(ws: WsCore, t: TCore, e: dict[str, Any]) -> Refusal | None:
    if not claims.signer_manages(ws, t, e):
        return Refusal(Code.ROLE_DENIED, "only the ticket owner, owners and maintainers change visibility")
    v = e["visibility"]
    if v != "workspace":
        if (r := identity.ref_refusal(ws, v["restricted"])) is not None:
            return r
        unknown = [p for p in v["restricted"] if not identity.known(ws, p)]
        if unknown:
            return Refusal(Code.MEMBER_UNKNOWN, f"not members: {unknown}")
    t.fields["visibility"] = v
    return None
