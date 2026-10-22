"""Canonical identities (hub contract 9.1, spec D59): a hub user ``u_x`` and the person ``p_y`` the owner aliased it to
in ``hub.joined`` are one person. Every separation-of-duties comparison (assignee vs approver, a worker vs a verdict,
one counting approval per person, who may manage a ticket) goes through ``canonical`` so the other identity cannot
be used to dodge it. A workspace without a hub has no aliases and every id maps to itself (D79)."""

from __future__ import annotations

import re
from collections.abc import Iterable

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
    hub contract 9.1). The ONE place hub actors are resolved: C3 never calls :func:`canonical` on a hub-signed id.

    ``user_id`` is the bare 32-hex id (the event's ``u_`` prefix removed). A user id is random and chosen by a hub,
    so ``u_x`` names a user only together with a hub. Because :func:`canonical` resolves over the union of every hub
    ever joined (so history keeps merging, D59), it would turn a user id that hub A aliased to ``p_mara`` into
    ``p_mara`` for hub B as well. A hub may not mint another hub's ids, so this refuses:

    * a workspace not currently joined to ``hub_id`` (``hub.invalid``);
    * a ``user_id`` that ANOTHER hub's alias book aliases and that ``hub_id``'s own book does not (``hub.invalid``).
      The same person aliased under both hubs is fine: it is in the hub's own book.

    Otherwise ``canonical(ws, "u_" + user_id)``: ``p_<person>`` when aliased by this hub, else ``u_<user_id>``."""
    if type(hub_id) is not str or type(user_id) is not str or not _HEX32.fullmatch(user_id):
        return Refusal(Code.HUB_INVALID, "malformed hub actor")
    h = ws.hub
    if not h or h.get("status") != "joined" or h.get("hub_id") != hub_id:
        return Refusal(Code.HUB_INVALID, "this workspace is not joined to that hub")
    book = h.get("alias_book", {})
    own = set(book.get(hub_id, {}).values())
    if user_id not in own and any(user_id in al.values() for hid, al in book.items() if hid != hub_id):
        return Refusal(Code.HUB_INVALID, "this user id is aliased under another hub; a hub may not mint its ids")
    return canonical(ws, "u_" + user_id)
