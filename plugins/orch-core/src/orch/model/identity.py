"""Canonical identities (hub contract 9.1, spec D59): a hub user ``u_x`` and the person ``p_y`` the owner aliased it to
in ``hub.joined`` are one person. Every separation-of-duties comparison (assignee vs approver, a worker vs a verdict,
one counting approval per person, who may manage a ticket) goes through ``canonical`` so the other identity cannot
be used to dodge it. A workspace without a hub has no aliases and every id maps to itself (D79)."""

from __future__ import annotations

from collections.abc import Iterable

from .types import WsCore


def canonical(ws: WsCore, ident: str) -> str:
    """``p_<person>`` for an aliased ``u_<user>`` of the hub joined now (none after ``hub.left``), else ``ident``."""
    if ident.startswith("u_"):
        aliases = ws.hub.get("aliases") if ws.hub and ws.hub.get("status") == "joined" else None
        if aliases:
            for pid, uid in aliases.items():
                if "u_" + uid == ident:
                    return "p_" + pid
    return ident


def canonical_set(ws: WsCore, idents: Iterable[str]) -> set[str]:
    return {canonical(ws, i) for i in idents}
