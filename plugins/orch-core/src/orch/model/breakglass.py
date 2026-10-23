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
from .types import WsCore, ts

__all__ = [
    "BREAK_GLASS", "BreakGlass", "SIGNER_KINDS", "agent_refusal", "allowed", "event_types", "human_only_types",
    "is_recovery", "local_refusal", "signer_kind",
]  # fmt: skip

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
    "hub.leave": BreakGlass("hub.left", ("owner_device_key",)),
    # L2-C8i-1: with no valid device left, the owner recovers one with the recovery key (D50 ``device.added``, only
    # in the recovery shape: :func:`is_recovery`) and that device signs the leave. A plain ``device.added`` is not pk.
    "device.recover": BreakGlass("device.added", ("pk",)),
    "hub.repin": BreakGlass("hub.repinned", ("pk",)),  # backlog (#876): listed so the set is complete, op refused
    "keys.rotate": BreakGlass(None, ("owner_device_key",)),
}


def event_types() -> frozenset[str]:
    return frozenset(r.event for r in BREAK_GLASS.values() if r.event is not None)


def allowed(verb: str, signer: str) -> bool:
    """May ``signer`` (a kind of :data:`SIGNER_KINDS`) do ``verb``? Unknown verb or kind: no."""
    row = BREAK_GLASS.get(verb)
    return row is not None and signer in SIGNER_KINDS and signer in row.signers


def is_recovery(ws: WsCore, e: Mapping[str, Any]) -> bool:
    """The D50 recovery shape, the ONE predicate (``authz.authorize_person`` and :func:`signer_kind` both use it): a
    ``device.added`` signed by the new device itself, which the roster does not know, for a person with no valid device
    at ``at``. The line then carries the recovery certificate (``PK``)."""
    from .authz import person_has_valid_device

    a = e.get("actor", {})
    if e.get("type") != "device.added" or e.get("device") != a.get("device") or a.get("device") in ws.devices:
        return False
    if not isinstance(e.get("cert"), Mapping):
        return False
    return not person_has_valid_device(ws, a.get("id"), ts(e["at"]))


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
    if is_recovery(ws, e):
        return "pk"
    dev = ws.devices.get(a.get("device"))
    return "owner_device_key" if dev is not None and dev.person == a.get("id") else "other"


# ``other`` is deliberately not a member of SIGNER_KINDS: nothing allows it.
_AGENT_ONLY_TYPES: frozenset[str] | None = None


def human_only_types() -> frozenset[str]:
    """Event types only a person's operation emits (``emits.HUMAN_ONLY`` minus what any agent operation emits)."""
    global _AGENT_ONLY_TYPES
    if _AGENT_ONLY_TYPES is None:
        from .emits import EMITS_V1, HUMAN_ONLY

        human = frozenset().union(*(EMITS_V1[o] for o in HUMAN_ONLY if o in EMITS_V1))
        agent = frozenset().union(*(t for o, t in EMITS_V1.items() if o not in HUMAN_ONLY))
        _AGENT_ONLY_TYPES = human - agent
    return _AGENT_ONLY_TYPES


def agent_refusal(ws: WsCore, e: dict[str, Any]) -> Refusal | None:
    """The same choke point for agents (C8, orchestrator note): in a joined full-mode position an agent line of a
    human-only type (a decision, a close, a grant) is refused, however its grant reads.
    Agents keep every other write."""
    h = ws.hub
    if not h or h.get("status") != "joined" or h.get("mode") != "full":
        return None
    if e["type"] in human_only_types():
        return Refusal(Code.SIGN_HUB_ONLY, f"{e['type']} is signed through the hub in a full-mode workspace")
    return None


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
    if signer_kind(ws, e) not in row.signers:
        return Refusal(Code.SIGN_HUB_ONLY, f"{typ} is break-glass: not by this way of signing")
    return None
