"""Break-glass (V3 C8, design-c8 M1; the set C11 imports): the few acts an owner may do locally in a joined hub
workspace, because the hub's role gate (D78) must not be bypassable and the hub must not be able to lock the owner out.

``BREAK_GLASS`` is the one table: verb -> {event type, allowed signers}. A signer kind is *how* the act was signed:
``owner_device_key`` (the owner's passphrase device key at the terminal), ``pk`` (the owner's recovery key), and the
kinds that are never allowed here: ``click`` (the dashboard click signer), ``convenience``, ``agent``, ``hub``
(``auth: "hub"``) and ``relay`` (a phone paired directly). A kind that is not in a verb's list is refused, so the
table is fail closed: adding a verb is adding a row.

``keys.rotate`` has no event (a key-store act), so its enforcement is the op catalog (human only, column T, owner),
not the event-type check below. ``hub.repin`` is listed so the set is complete; its op is not built yet, and it is
``pk`` only (the matrix test already refuses it by a device key).

The model rule that applies it at append and replay (``local_refusal``, ``agent_refusal``) is C8's; C11's
host enforces the same table at ``human.append_signed`` (``host/hubsign.py``). D79: no hub or host code here.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from .types import WsCore

__all__ = ["BREAK_GLASS", "BreakGlass", "SIGNER_KINDS", "allowed", "event_types", "signer_kind"]

SIGNER_KINDS = ("owner_device_key", "pk", "click", "convenience", "agent", "hub", "relay")


class BreakGlass:
    """One row: the event the verb writes (``None``: a key-store act without an event) and who may sign it."""

    __slots__ = ("event", "signers")

    def __init__(self, event: str | None, signers: tuple[str, ...]) -> None:
        self.event, self.signers = event, signers

    def __repr__(self) -> str:  # pragma: no cover
        return f"BreakGlass({self.event!r}, {self.signers!r})"


#: verb -> row. Owner only in every row (checked separately: the actor must be a workspace owner).
BREAK_GLASS: Mapping[str, BreakGlass] = {
    "hub.leave": BreakGlass("hub.left", ("owner_device_key", "pk")),
    "hub.repin": BreakGlass("hub.repinned", ("pk",)),  # backlog (#876): listed so the set is complete, op refused
    "keys.rotate": BreakGlass(None, ("owner_device_key", "pk")),
}


def event_types() -> frozenset[str]:
    return frozenset(r.event for r in BREAK_GLASS.values() if r.event is not None)


def allowed(verb: str, signer: str) -> bool:
    """May ``signer`` (a kind of :data:`SIGNER_KINDS`) do ``verb``? Unknown verb or kind: no."""
    row = BREAK_GLASS.get(verb)
    return row is not None and signer in SIGNER_KINDS and signer in row.signers


def _recovery_signature(ws: WsCore, e: Mapping[str, Any]) -> bool:
    """The recovery key ``PK`` signed: the actor's device is not a device of the roster and the line carries the
    recovery certificate (the shape ``authorize_person`` calls recovery). Same rule as C11's ``hubsign``."""
    return e["actor"].get("device") not in ws.devices and isinstance(e.get("cert"), Mapping)


def signer_kind(ws: WsCore, e: Mapping[str, Any]) -> str:
    """The kind of signer of a person event, read from the line and the roster, **fail closed**: only a passphrase (or
    unlabelled) event with no ``signed_by`` is ever the owner's device key (a device of this actor's own) or the
    recovery key. Every other label, a device of someone else, or a device the roster does not know is ``other``,
    which no row of :data:`BREAK_GLASS` allows."""
    a = e.get("actor", {})
    if a.get("kind") == "agent":
        return "agent"
    if "via" in a or e.get("auth") == "hub":
        return "hub"
    if e.get("auth") == "convenience":
        return "convenience"
    if e.get("signed_by"):
        return "relay"
    if e.get("auth") not in (None, "passphrase"):
        return "other"  # a phone's label, webauthn, ...: not the terminal
    if _recovery_signature(ws, e):
        return "pk"
    dev = ws.devices.get(a.get("device"))
    return "owner_device_key" if dev is not None and dev.person == a.get("id") else "other"
