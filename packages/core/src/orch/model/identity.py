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


def _snapshot(ws: WsCore) -> Any:
    h = ws.hub
    return (h.get("ledger", {}).get(h.get("hub_id"), {}) or {}).get("snapshot")


def _snapshot_role(ws: WsCore, user_id: str) -> str | None:
    snap = _snapshot(ws)
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


def member_role(ws: WsCore, person: str) -> str | None:
    """The effective role of the workspace member ``person`` (``p_...``) for what its AGENTS may do (#1133).

    Standalone, hub left, or selective mode: the member's role. Full mode: the **lower** of the member's role and the
    hub role of the user aliased to ``person`` in the joined hub's book, ``None`` when no user is aliased to it or the
    hub gave that user no role (removed from the group). The one place agent authority reads the hub projection."""
    m = ws.members.get(person)
    if m is None:
        return None
    h = ws.hub
    if not h or h.get("status") != "joined" or h.get("mode") != "full":
        return m.role
    if _snapshot(ws) is None:  # no snapshot pinned yet: the hub has taken nothing away, the member's role stands
        return m.role
    uid = (h.get("alias_book", {}).get(h.get("hub_id"), {}) or {}).get(person[2:])
    hub_role = _snapshot_role(ws, uid) if type(uid) is str else None
    return None if hub_role is None else _lower(m.role, hub_role)


def persons(ws: WsCore) -> set[str]:
    """Every canonical person who counts as a participant now: the members, plus (full mode) every hub user the pinned
    snapshot gives a role, aliased ones merged with their member (#1134)."""
    out = set(ws.members)
    h = ws.hub
    if h and h.get("status") == "joined" and h.get("mode") == "full":
        snap = _snapshot(ws)
        for uid in (snap or {}).get("o", {}).get("roles", {}) or {}:
            if _snapshot_role(ws, uid) is not None:
                out.add(canonical(ws, "u_" + uid))
    return out


LOOKUP: Any = object()  # "look the role up with role_of" (the default of the role parameters of the type rules)


def known(ws: WsCore, ident: str) -> bool:
    """``ident`` has a role now: a member, or a hub user the pinned snapshot gives one (``role_of`` is not ``None``)."""
    return role_of(ws, ident) is not None


def ref_refusal(ws: WsCore, idents: Iterable[str]) -> Refusal | None:
    """A ``u_`` person reference (``personRef``, ticket-format 15.1) is accepted only while a hub is joined at this
    position: ``hub.invalid`` otherwise, so a standalone workspace (D79) never holds one."""
    h = ws.hub
    if any(i.startswith("u_") for i in idents) and not (h and h.get("status") == "joined"):
        return Refusal(Code.HUB_INVALID, "not_joined")
    return None


def _at(node: Any, path: tuple[str, ...]) -> Iterable[Any]:
    if not path:
        yield node
    elif path[0] == "*":
        for v in node if isinstance(node, list) else ():
            yield from _at(v, path[1:])
    elif isinstance(node, Mapping) and path[0] in node:
        yield from _at(node[path[0]], path[1:])


def u_refs(e: Mapping[str, Any]) -> list[str]:
    """Every bare 32-hex hub user id an event uses: its actor and every ``u_`` person reference in its payload. The
    payload fields are not listed by hand: they are the ``personRef`` places of the event's own schema
    (``orch.schema.person_ref_paths``), so a new field is covered the day it is added to the schema."""
    from orch.schema import person_ref_paths  # local: the schema package is data, the model stays importable alone

    out: list[Any] = [e["actor"]["id"]] if "via" in e["actor"] else []
    for path in person_ref_paths("event." + str(e["type"])):
        out += _at(e, path)
    return sorted({i[2:] for i in out if type(i) is str and i.startswith("u_")})


def record_users(ws: WsCore, e: Mapping[str, Any]) -> Refusal | None:
    """An admitted event records every ``u_`` id it uses under a hub in ``ws.hub["user_hub"]`` (append-only, first
    writer wins), so a later event of ANOTHER hub that presents one of them is ``hub.foreign_user`` (L2P4-C1, §15.6)
    without any state injected by a join. The hub is the event's own ``hub_id`` for a hub-signed event (actor and
    payload references), and the hub joined at this position for any other event's payload references (§15.6 case 2:
    a member's device event naming a hub user). Every reference is checked first (``hub_actor``); an event with no
    ``u_`` id, or a device event with no hub joined (the type rules refuse that), records nothing."""
    ids = u_refs(e)
    if not ids:
        return None
    h = ws.hub
    hub_id = e["hub_id"] if "via" in e["actor"] else (h.get("hub_id") if h and h.get("status") == "joined" else None)
    if hub_id is None:
        return None
    for uid in ids:
        who = hub_actor(ws, hub_id, uid)
        if isinstance(who, Refusal):
            return who
    book = ws.hub.setdefault("user_hub", {})
    for uid in ids:
        book.setdefault(uid, hub_id)
    return None


def signer(ws: WsCore, e: Mapping[str, Any]) -> Signer | Refusal:
    """The :class:`Signer` of a person event. The ONE place a person actor's identity, role and key are derived: no
    consumer reads ``actor.id``, ``actor.device`` or ``ws.members[...]`` for a person actor directly (L2P4-C2)."""
    a = e["actor"]
    if "via" in a:
        who = hub_actor(ws, e["hub_id"], a["id"][2:])
        if isinstance(who, Refusal):
            return who
        return Signer(who, role_of(ws, a["id"]), f"hub:{e['hub_id']}/{e['channel']}")
    return Signer(canonical(ws, a["id"]), role_of(ws, a["id"]), a.get("device", ""))
