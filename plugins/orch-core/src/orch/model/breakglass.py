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

Model rule (:func:`local_refusal`, called from ``authz.authorize_person`` at append and on replay): in a joined
**full**-mode position every person event that is not ``auth: "hub"`` is refused with ``sign.hub_only``, except the
event types of ``BREAK_GLASS``, which also need an owner actor and a signer that is allowed. Exclusion never applies:
a ``device.excluded`` line never reaches this check (and core refuses excluding an owner's device, C10).
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from .codes import Code, Refusal
from .types import WsCore

__all__ = ["BREAK_GLASS", "BreakGlass", "SIGNER_KINDS", "allowed", "event_types", "local_refusal", "signer_kind"]

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


def signer_kind(e: Mapping[str, Any]) -> str:
    """The kind of signer a person event carries, from the line itself (never from a label the caller chooses).
    ``auth: "hub"`` is the hub, ``convenience`` the click signer, a ``signed_by`` the relay; anything else is the
    owner's device key (a recovery-key signature is indistinguishable at this level and is judged by the host)."""
    a = e.get("actor", {})
    if a.get("kind") == "agent":
        return "agent"
    if e.get("auth") == "hub":
        return "hub"
    if e.get("auth") == "convenience":
        return "convenience"
    if e.get("signed_by"):
        return "relay"
    return "owner_device_key"


def local_refusal(ws: WsCore, e: dict[str, Any]) -> Refusal | None:
    """The model's D78 rule for one person event (see the module docstring). ``None``: not in a joined full-mode
    position, or a break-glass event done as allowed."""
    h = ws.hub
    if not h or h.get("status") != "joined" or h.get("mode") != "full" or e.get("auth") == "hub":
        return None
    typ = e["type"]
    row = next((r for r in BREAK_GLASS.values() if r.event == typ), None)
    if row is None:
        return Refusal(Code.SIGN_HUB_ONLY, f"{typ} is signed through the hub in a full-mode workspace")
    m = ws.members.get(e["actor"]["id"])
    if m is None or m.role != "owner":
        return Refusal(Code.SIGN_HUB_ONLY, f"{typ} is break-glass: the owner only")
    if signer_kind(e) not in row.signers:
        return Refusal(Code.SIGN_HUB_ONLY, f"{typ} is break-glass: not by this way of signing")
    return None
