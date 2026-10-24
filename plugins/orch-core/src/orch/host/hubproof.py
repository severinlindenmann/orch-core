"""The owner proof of a hub step at the host (#1198 C9, hub contract 9.6 "At the host").

``hub.join``, ``hub.mode`` and ``hub.leave`` each append an owner-signed event (``hub.joined`` / ``hub.left``) that
carries an ``owner_proof``: a statement signed by the owner's person key ``PK`` (derived from the backup key) over the
exact payload the owner reviewed, bound to this workspace, this op and this request, and good for at most 10 minutes.
A device key, even the owner's own, cannot make one, so a stolen One-click key or a taken-over dashboard cannot bind
the workspace to a hub. This is the ONE place the host checks it; there is no path that skips it:

* ``human.get`` with a signer (binding): a proof sent with the bind is checked; a One-click (``convenience``) signer
  that sends none is refused (``proof_missing``), because it could never produce one later.
* ``human.submit`` (under the store lock, with the roster as it is now): every hub event must carry a valid proof.
* the model (``orch.model.workspace.hub_owner_proof``): every ``hub.joined`` / ``hub.left`` that is appended or
  replayed, whatever its ``auth`` and whoever delivered it.

Every check takes the acting owner's ``pk_pub`` from the roster (never from the request or the event) and the request
id being bound or submitted. Every refusal is ``hub.proof_invalid`` with the contract code as ``detail.reason``.
"""

from __future__ import annotations

from typing import Any

from orch.crypto.encoding import EncodingError, unb64u
from orch.hubclient import wire
from orch.hubclient.errors import HubRefused

from .errors import HostError

__all__ = ["OPS", "check", "payload_of", "refusal"]

#: the ops that append a hub event (the type is fixed by the op: hub.leave -> hub.left, the others -> hub.joined)
OPS = frozenset(wire.OWNER_PROOF_OPS)
_REASONS = ("proof_invalid", "proof_expired", "proof_missing", "dev_mismatch", "dev_refused")


def refusal(code: str) -> HostError:
    """``hub.proof_invalid`` for the contract code ``code`` (anything unknown counts as ``proof_invalid``)."""
    reason = code if code in _REASONS else "proof_invalid"
    return HostError(
        "hub.proof_invalid",
        f"the hub step needs your backup key: {reason}",
        hint="sign the hub step again at the terminal with your backup key: orch sign",
        fix=["orch", "sign"],
        detail={"reason": reason},
    )


def payload_of(event: dict[str, Any]) -> dict[str, Any]:
    """The event's contract members without ``owner_proof``: what the proof is over."""
    keys = wire.LEFT_KEYS if event.get("type") == "hub.left" else wire.JOINED_KEYS
    return {k: event[k] for k in keys if k in event}


def check(service: Any, *, person: str, op: str, request_id: str, event: dict[str, Any], proof: Any) -> None:
    """Refuse (``hub.proof_invalid``) unless ``proof`` is a valid owner proof of ``person`` for ``op`` over ``event``'s
    payload, bound to ``request_id``, at the host's clock and dev state. ``person`` is the request's person."""
    from . import hubops

    if proof is None:
        raise refusal("proof_missing")
    state = service.store.state
    view = state.workspace.members.get(person)
    key = state.person_key(person)  # the roster's PK (b64u) of a current member; never a request or event field
    try:
        if view is None or view.role != "owner" or key is None:
            raise HubRefused("proof_invalid")
        pk_pub = unb64u(key, 65)
        wire.check_owner_proof(
            proof, pk_pub=pk_pub, workspace_id=service.workspace_id, op=op, payload=payload_of(event),
            request_id=request_id, now_ms=int(service.clock() * 1000), dev_mode=bool(hubops.workspace_dev(service)),
        )  # fmt: skip
    except (HubRefused, EncodingError) as exc:
        raise refusal(getattr(exc, "code", "proof_invalid")) from None
