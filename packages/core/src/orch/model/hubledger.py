"""The hub ledger (ticket-format §15.2): the hub material a workspace has pinned, as it was at each log position.

``hub.pinned`` lines put one signed hub object (card, op revocation or snapshot) into the workspace log. This module
applies them to ``ws.hub["ledger"][hub_id]`` with the same rules at append and on replay, so a custodial line is
always checked against the material of its own position and ``at`` (§15.3), never today's. The model does no crypto
(D79: nothing from the hub repository, no hubclient import): the signature and chain checks are the injected
``Verifier.verify_hub_pin``; this module owns the ledger state, the monotone rules and the caps.

Ledger state, per hub id, kept beside ``alias_book`` and in every checkpoint with ``ws.hub`` (survives ``hub.left``
and a rejoin)::

    {root, card, n_cards, revoked_op_certs, snapshot, revoked_channels}

``root`` is the ``hk_root_pub`` of the first ``hub.joined`` of that hub. ``card`` and ``snapshot`` are the signed
objects, verbatim; ``revoked_op_certs`` and ``revoked_channels`` are sorted lists that only grow (a channel revocation
is permanent, hub contract 6.2).
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from orch.canon.jcs import dumps as cj

from .codes import Code, Refusal
from .types import WsCore, ts
from .verifier import Verifier

__all__ = [
    "MAX_CARDS",
    "MAX_OP_REVOCATIONS",
    "MAX_SNAPSHOT_BYTES",
    "apply_pin",
    "channel_revoked",
    "ledger_of",
    "new_ledger",
    "pinned_snapshot",
]

#: §15.2 caps, per hub: the ledger is bounded, nothing is ever pruned.
MAX_OP_REVOCATIONS = 64
MAX_CARDS = 16
MAX_SNAPSHOT_BYTES = 64 * 1024


def new_ledger(root: str) -> dict[str, Any]:
    return {"root": root, "card": None, "n_cards": 0, "revoked_op_certs": [], "snapshot": None, "revoked_channels": []}


def ledger_of(ws: WsCore, hub_id: str) -> dict[str, Any] | None:
    led = ws.hub.get("ledger", {}).get(hub_id) if ws.hub else None
    return led if isinstance(led, dict) else None


def pinned_snapshot(led: Mapping[str, Any]) -> Mapping[str, Any] | None:
    """The ``o`` of the newest pinned snapshot, or ``None``."""
    s = led.get("snapshot")
    return s["o"] if s else None


def channel_revoked(led: Mapping[str, Any], channel: str) -> bool:
    return channel in led["revoked_channels"]


def _bad(code: str) -> Refusal:
    return Refusal(Code.HUB_INVALID, code)


def apply_pin(ws: WsCore, e: dict[str, Any], v: Verifier, *, dev: bool) -> Refusal | None:
    """Rules 1-4 of §15.2 for one ``hub.pinned`` line, mutating ``ws.hub`` (the caller passes a scratch copy). A
    failure is ``hub.invalid`` with the contract code as detail (rule 5)."""
    h = ws.hub
    hub_id, obj = e["hub_id"], e["object"]
    if not h or h.get("status") != "joined" or h.get("hub_id") != hub_id:
        return _bad("not_joined")  # rule 1; also a standalone workspace (D79)
    led = ledger_of(ws, hub_id)
    if led is None:
        return _bad("not_joined")
    kind = obj["o"]["kind"]
    if obj["o"].get("hub_id") != hub_id:
        return _bad("other_hub")
    check = getattr(v, "verify_hub_pin", None)
    if check is None:
        return _bad("unavailable")  # fail closed: a verifier without the hub methods verifies nothing
    at_ms = ts(e["at"]) * 1000
    revoked = frozenset(led["revoked_op_certs"])

    def verified(card: Any, snapshot: Any) -> str | None:
        return check(
            obj, root=led["root"], card=card, revoked=revoked, snapshot=snapshot,
            workspace_id=ws.workspace_id, at_ms=at_ms, dev=dev,
        )  # fmt: skip

    if kind == "hub.card":
        if led["card"] is not None and cj(obj) == cj(led["card"]):
            return _bad("already_pinned")
        if (code := verified(led["card"], led["snapshot"])) is not None:
            return _bad(code)
        if led["n_cards"] >= MAX_CARDS:
            return _bad("too_many")
        led["card"], led["n_cards"] = obj, led["n_cards"] + 1
        return None
    if kind == "hub.op_revoked":
        if (code := verified(led["card"], led["snapshot"])) is not None:
            return _bad(code)
        cert = obj["o"]["cert_id"]
        if cert in revoked:
            return _bad("already_pinned")
        if len(revoked) >= MAX_OP_REVOCATIONS:
            return _bad("too_many")
        led["revoked_op_certs"] = sorted({*revoked, cert})
        return None
    if kind == "hub.snapshot":
        if led["card"] is None:
            return _bad("no_card")
        if (code := verified(led["card"], led["snapshot"])) is not None:
            return _bad(code)  # includes stale_snapshot and snapshot_conflict against the pinned one
        held = led["snapshot"]
        if held is not None and held["o"]["version"] == obj["o"]["version"]:
            return _bad("already_pinned")  # the verifier refused other bytes of this version already
        if len(cj(obj)) > MAX_SNAPSHOT_BYTES:
            return _bad("too_large")
        led["snapshot"] = obj
        led["revoked_channels"] = sorted({*led["revoked_channels"], *obj["o"]["revoked_channels"]})
        return None
    return _bad("malformed")
