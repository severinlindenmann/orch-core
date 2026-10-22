"""The hub audit chain (protocol.md §10.5, D4; spec §5 "Audit"). Pure functions.

One global chain per hub. Record ``n`` is a ``hub.audit`` object signed by ``HK_op`` that names the previous record's
hash, so removing, reordering or rewriting a record breaks every later one. A record carries the HASH of its event, not
the event: a workspace verifies the whole chain (every record, even those about others) and sees the events that concern
it, and learns nothing about the rest. ``audit_ack`` tells the hub which head a workspace verified.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from . import suite as S
from .canon import CanonError, b64u, cj
from .hub import (
    Refused,
    _b64u_n,
    _bool,
    _enum,
    _hex16,
    _int_min,
    _under_card,
)
from .labels import L

GENESIS_PREV = b64u(bytes(32))
ROLES_OR_NONE = ("read", "edit", "admin", None)


def _text(x: Any) -> bool:
    return type(x) is str and 0 < len(x) <= 128 and x.isprintable()


def _opt_hex16(x: Any) -> bool:
    return x is None or _hex16(x)


#: Exact event fields per record type. ``scope`` of the record is the event's ``workspace_id`` (None when it has none or
#: it is None).
EVENT_FIELDS: dict[str, dict[str, Any]] = {
    # every custodial signature (§8): who signed what, over which digest
    "custodial": {
        "workspace_id": _hex16,
        "log": _text,
        "verb": _text,
        "user_id": _hex16,
        "channel_id": _hex16,
        "digest": _b64u_n(32),
    },
    # a group's role in a workspace changed (role None = removed); the acting admin
    "grant": {
        "group": _text,
        "workspace_id": _hex16,
        "role": lambda x: x in ROLES_OR_NONE,
        "actor_user_id": _hex16,
    },
    # a user joined or left a group; the acting admin
    "membership": {"group": _text, "user_id": _hex16, "added": _bool, "actor_user_id": _hex16},
    # a space changed (create, add, remove, unbind, delete); the epoch it produced
    "space": {
        "space_id": _hex16,
        "epoch": _int_min(1),
        "change": _enum("create", "add", "remove", "unbind", "delete"),
        "workspace_id": _opt_hex16,
        "actor_user_id": _opt_hex16,
    },
}


def event_hash(event: Mapping[str, Any]) -> str:
    try:
        return b64u(S.sha256(L["h_hub_audit_event"] + cj(dict(event))))
    except CanonError:
        raise Refused("malformed") from None


def chain_hash(signed: Mapping[str, Any]) -> str:
    """The hash the next record's ``prev`` and an ack's ``head`` carry: over the whole signed record."""
    try:
        return b64u(S.sha256(L["h_hub_audit_chain"] + cj(dict(signed))))
    except CanonError:
        raise Refused("malformed") from None


def check_event(typ: str, event: Any) -> dict[str, Any]:
    spec = EVENT_FIELDS.get(typ)
    if spec is None or type(event) is not dict or set(event) != set(spec):
        raise Refused("malformed")
    if not all(ok(event[k]) for k, ok in spec.items()):
        raise Refused("malformed")
    return event


def scope_of(event: Mapping[str, Any]) -> str | None:
    return event.get("workspace_id")


def verify_record(
    signed: Any,
    held: Mapping[str, Any],
    *,
    dev_mode: bool,
    event: Any = None,
    revoked_op_certs: frozenset[str] = frozenset(),
) -> dict[str, Any]:
    """The record under the held hub card. If the host has the event (it concerns the host), it must hash to
    ``event_hash``, be a valid event of the record's ``type`` and match its ``scope``: ``audit_mismatch`` otherwise."""
    o = _under_card(signed, "hub.audit", held, dev_mode, revoked_op_certs)
    if o["seq"] == 1 and o["prev"] != GENESIS_PREV:
        raise Refused("malformed")
    if event is not None:
        if event_hash(event) != o["event_hash"]:
            raise Refused("audit_mismatch")
        check_event(o["type"], event)
        if scope_of(event) != o["scope"]:
            raise Refused("audit_mismatch")
    return o


def apply_record(head: Mapping[str, Any] | None, signed: Mapping[str, Any], o: Mapping[str, Any]) -> dict[str, Any]:
    """Chain rule. ``head`` = ``{"seq", "hash"}`` of the last record the verifier accepted, or None (it expects
    genesis). Next record: ``seq = head.seq + 1`` and ``prev = head.hash``. A skipped ``seq`` is ``audit_gap``; the right
    ``seq`` with another ``prev``, or an old ``seq`` with other bytes, is ``audit_fork`` (the hub told two stories). An
    old ``seq`` with the same bytes is idempotent."""
    h = chain_hash(signed)
    if head is None:
        if o["seq"] != 1:
            raise Refused("audit_gap")
        return {"seq": 1, "hash": h}
    if o["seq"] <= head["seq"]:
        if o["seq"] == head["seq"] and h == head["hash"]:
            return dict(head)
        raise Refused("audit_fork")  # an older record cannot be checked against the head: other bytes at a taken seq
    if o["seq"] > head["seq"] + 1:
        raise Refused("audit_gap")
    if o["prev"] != head["hash"]:
        raise Refused("audit_fork")
    return {"seq": o["seq"], "hash": h}


def check_ack(
    chain: list[Mapping[str, Any]], acked: Mapping[str, int], workspace_id: str, body: Mapping[str, Any]
) -> dict[str, Any]:
    """The hub's side of ``audit_ack {seq, head}`` from ``workspace_id``: ``chain`` = the hub's signed records, oldest
    first (record ``n`` at index ``n - 1``). The head must be the hash of the hub's own record ``seq``; anything else
    (a seq the hub never wrote, another hash) is ``audit_fork``: the workspace holds a chain the hub did not sign
    there. Acks only rise; an older ack is a no-op (the stored one stays). Returns ``{"acked": seq}``. SHORTCUT: the
    hub records acks and never acts on a missing or stale one (L7)."""
    seq = body["seq"]
    if seq > len(chain) or chain_hash(chain[seq - 1]) != body["head"]:
        raise Refused("audit_fork")
    return {"acked": max(seq, acked.get(workspace_id, 0))}
