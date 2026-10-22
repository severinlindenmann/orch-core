"""Canonical identities (hub contract 9.1, spec D59): a hub user ``u_x`` and the person ``p_y`` the owner aliased it to
in ``hub.joined`` are one person. Every separation-of-duties comparison (assignee vs approver, a worker vs a verdict,
one counting approval per person, who may manage a ticket) goes through ``canonical`` so the other identity cannot
be used to dodge it. A workspace without a hub has no aliases and every id maps to itself (D79)."""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from .codes import Code, Refusal
from .types import WsCore

_HEX32 = re.compile(r"[0-9a-f]{32}")


def canonical(ws: WsCore, ident: str) -> str:
    """``p_<person>`` for a ``u_<user>`` aliased under ANY hub this workspace ever joined, else ``ident``.

    The alias book is append-only and ``hub_joined`` keeps it a function (a user id never names two people), so the
    union is stable: history recorded as ``u_x`` (workers, decisions, owners) keeps merging with ``p_y`` after
    ``hub.left`` and after another hub is joined (L2P4-C1b, else the owner could approve their own work, D59).
    This is for IDS ALREADY IN THE LOG (history). An actor on an INCOMING hub event goes through :func:`hub_actor`."""
    if ident.startswith("u_") and ws.hub:
        for aliases in ws.hub.get("alias_book", {}).values():
            for pid, uid in aliases.items():
                if "u_" + uid == ident:
                    return "p_" + pid
    return ident


def canonical_set(ws: WsCore, idents: Iterable[str]) -> set[str]:
    return {canonical(ws, i) for i in idents}


def hub_actor(ws: WsCore, hub_id: str, user_id: str) -> str | Refusal:
    """The canonical identity of the actor of an incoming ``auth: "hub"`` event signed by hub ``hub_id`` (L2P4-C1,
    hub contract 9.1, ticket-format 15.6). The ONE place hub actors are resolved: C3 never calls :func:`canonical` on
    a hub-signed id.

    ``user_id`` is the bare 32-hex id (the event's ``u_`` prefix removed). A user id is random and chosen by a hub,
    so ``u_x`` names a user only together with a hub, and **belongs to the hub under which it first appeared in this
    workspace** (``ws.hub["user_hub"]``, append-only). This refuses:

    * a workspace not currently joined to ``hub_id`` (``hub.invalid``);
    * a ``user_id`` that belongs to ANOTHER hub (``hub.foreign_user``): first recorded under it, or aliased in its
      alias book. The one exception is the same person aliased on both hubs (``hub_id``'s own book and the other
      hub's book map the id to the same person). An id first recorded unaliased under another hub never qualifies.

    Otherwise ``canonical(ws, "u_" + user_id)``: ``p_<person>`` when aliased, else ``u_<user_id>``."""
    if type(hub_id) is not str or type(user_id) is not str or not _HEX32.fullmatch(user_id):
        return Refusal(Code.HUB_INVALID, "malformed hub actor")
    h = ws.hub
    if not h or h.get("status") != "joined" or h.get("hub_id") != hub_id:
        return Refusal(Code.HUB_INVALID, "not_joined")
    book = h.get("alias_book", {})
    first = h.get("user_hub", {}).get(user_id)
    named = {hid: p for hid, al in book.items() for p, u in al.items() if u == user_id}
    others = {hid: p for hid, p in named.items() if hid != hub_id}
    if (first is not None and first != hub_id) or others:
        own = named.get(hub_id)
        same_person = own is not None and bool(others) and all(p == own for p in others.values())
        recorded_ok = first is None or first == hub_id or book.get(first, {}).get(own) == user_id
        if not (same_person and recorded_ok):
            return Refusal(Code.HUB_FOREIGN_USER, "foreign_user")
    return canonical(ws, "u_" + user_id)


# --- who signed an event, and with which role (ticket-format 15.4) ----------------------------------------------------
#: the member roles in order, highest first; a hub snapshot's roles map onto them (D70: ``admin`` is today's owner)
ROLE_ORDER = ("owner", "maintainer", "member", "viewer")
HUB_ROLE = {"admin": "owner", "edit": "member", "read": "viewer"}


@dataclass(frozen=True)
class Signer:
    """``person`` is canonical (``p_...`` when aliased, else ``u_...``), ``key`` names what signed: a device id, or
    ``hub:<hub_id>/<channel>`` (never a ``d_`` id)."""

    person: str
    role: str | None
    key: str


def _lower(a: str, b: str) -> str:
    return a if ROLE_ORDER.index(a) >= ROLE_ORDER.index(b) else b


def _snapshot_role(ws: WsCore, user_id: str) -> str | None:
    h = ws.hub
    snap = (h.get("ledger", {}).get(h.get("hub_id"), {}) or {}).get("snapshot")
    role = ((snap or {}).get("o", {}).get("roles", {}) or {}).get(user_id)
    return HUB_ROLE.get(role) if type(role) is str else None


def role_of(ws: WsCore, ident: str) -> str | None:
    """The member role of ``ident`` at the current position, or ``None`` (no role at all).

    * ``p_...``: ``ws.members``.
    * a ``u_...`` aliased (any hub's book, the C4 union) to a ``p_...``: ``None`` when that person is not a member now
      (it never falls back to the unaliased branch: a person removed from the roster keeps no hub role); selective
      mode: the member's role (L2P4-1); full mode: the **lower** of the member's role and the snapshot role.
    * an unaliased ``u_...``: full mode: the mapped snapshot role; selective mode: ``None`` (not granted).
    * no hub joined: ``None`` for every ``u_...``."""
    if not ident.startswith("u_"):
        m = ws.members.get(ident)
        return m.role if m is not None else None
    h = ws.hub
    if not h or h.get("status") != "joined":
        return None
    person = canonical(ws, ident)
    if person != ident:
        m = ws.members.get(person)
        if m is None:
            return None
        if h.get("mode") != "full":
            return m.role
        hub_role = _snapshot_role(ws, ident[2:])
        return None if hub_role is None else _lower(m.role, hub_role)
    return _snapshot_role(ws, ident[2:]) if h.get("mode") == "full" else None


def signer(ws: WsCore, e: Mapping[str, Any]) -> Signer | Refusal:
    """The :class:`Signer` of a person event. The ONE place a person actor's identity, role and key are derived: no
    consumer reads ``actor.id``, ``actor.device`` or ``ws.members[...]`` for a person actor directly (L2P4-C2)."""
    a = e["actor"]
    if "via" in a:
        who = hub_actor(ws, e["hub_id"], a["id"][2:])
        if isinstance(who, Refusal):
            return who
        return Signer(who, role_of(ws, a["id"]), f"hub:{e['hub_id']}/{e['channel']}")
    return Signer(a["id"], role_of(ws, a["id"]), a["device"])
