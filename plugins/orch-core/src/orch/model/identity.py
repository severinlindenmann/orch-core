"""Canonical identities (hub contract 9.1, spec D59): a hub user ``u_x`` and the person ``p_y`` the owner aliased it to
in ``hub.joined`` are one person. Every separation-of-duties comparison (assignee vs approver, a worker vs a verdict,
one counting approval per person, who may manage a ticket) goes through ``canonical`` so the other identity cannot
be used to dodge it. A workspace without a hub has no aliases and every id maps to itself (D79)."""

from __future__ import annotations

from collections.abc import Iterable

from .types import WsCore


def canonical(ws: WsCore, ident: str) -> str:
    """``p_<person>`` for a ``u_<user>`` aliased under ANY hub this workspace ever joined, else ``ident``.

    The alias book is append-only and ``hub_joined`` keeps it a function (a user id never names two people), so the
    union is stable: history recorded as ``u_x`` (workers, decisions, owners) keeps merging with ``p_y`` after
    ``hub.left`` and after another hub is joined (L2P4-C1b, else the owner could approve their own work, D59).
    TODO(C3): the host must refuse a hub event that uses a user id aliased under another hub (a hub may not mint
    another hub's ids); that check belongs where hub events enter, not here."""
    if ident.startswith("u_") and ws.hub:
        for aliases in ws.hub.get("alias_book", {}).values():
            for pid, uid in aliases.items():
                if "u_" + uid == ident:
                    return "p_" + pid
    return ident


def canonical_set(ws: WsCore, idents: Iterable[str]) -> set[str]:
    return {canonical(ws, i) for i in idents}
