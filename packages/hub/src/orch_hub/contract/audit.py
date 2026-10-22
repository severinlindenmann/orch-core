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
from .canon import CanonError, b64u, cj, unb64u, unhex
from .hub import (
    Refused,
    _b64u_n,
    _bool,
    _dev,
    _enum,
    _envelope,
    _fields,
    _hex16,
    _int_min,
    _signature,
    verify_op_cert,
)
from .labels import L

GENESIS_PREV = b64u(bytes(32))
ROLES_OR_NONE = ("read", "edit", "admin", None)


def _text(x: Any) -> bool:
    return type(x) is str and 0 < len(x) <= 128 and x.isprintable()


def _opt_hex16(x: Any) -> bool:
    return x is None or _hex16(x)


#: Exact event fields per record type. The record's ``scope_tag`` blinds the event's ``workspace_id`` with the event's
#: own ``scope_nonce`` (L2P-4): only the workspace the event is about receives the event and can check the tag; every
#: other workspace sees an opaque tag. ``None`` for hub-wide events.
EVENT_FIELDS: dict[str, dict[str, Any]] = {
    # every custodial signature (§8): who signed what, over which digest
    "custodial": {
        "workspace_id": _hex16,
        "log": _text,
        "verb": _text,
        "user_id": _hex16,
        "channel_id": _hex16,
        "digest": _b64u_n(32),
        "scope_nonce": _b64u_n(16),
    },
    # a group's role in a workspace changed (role None = removed); the acting admin
    "grant": {
        "group": _text,
        "workspace_id": _hex16,
        "role": lambda x: x in ROLES_OR_NONE,
        "actor_user_id": _hex16,
        "scope_nonce": _b64u_n(16),
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
        "scope_nonce": _b64u_n(16),
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


def scope_tag_of(event: Mapping[str, Any]) -> str | None:
    """``b64u(H("orch/v2/hub-audit-scope|" || workspace_id (16) || scope_nonce (16)))``, or None for a hub-wide event.
    The nonce is inside the event, which only the scope's workspace receives, so a co-tenant cannot test a guessed
    ``workspace_id`` against the tag."""
    ws = scope_of(event)
    if ws is None:
        return None
    try:
        return b64u(S.sha256(L["h_hub_audit_scope"] + unhex(ws, 16) + unb64u(event["scope_nonce"], 16)))
    except CanonError, KeyError:
        raise Refused("malformed") from None


def _record_key(
    o: Mapping[str, Any],
    held: Mapping[str, Any],
    op_certs: Any,
    dev_mode: bool,
    revoked_op_certs: frozenset[str],
    trusted_seq: int,
) -> tuple[bytes, int]:
    """The key a record verifies under and that op cert's ``op_seq`` (L2P-3). A record names the op cert it was signed
    under: the held card's current one, or an earlier one that the hub's root certified (``op_certs``, root-signed
    objects the hub serves with ``audit_get``), valid at the record's time. A revoked op cert still stands for records
    the verifier already passed (``seq <= trusted_seq``, covered by the hash chain) and for nothing newer."""
    cid, seq, ts = o.get("op_cert_id"), o.get("seq"), o.get("ts_ms")
    if not _hex16(cid) or type(seq) is not int or type(ts) is not int:
        raise Refused("malformed")
    if cid in revoked_op_certs and seq > trusted_seq:
        raise Refused("revoked")
    cur = held["op_cert"]
    if cid == cur["cert_id"]:
        return held["hk_op_pub"], cur["op_seq"]
    for c in op_certs or ():
        if type(c) is dict and type(c.get("o")) is dict and c["o"].get("cert_id") == cid:
            oc = verify_op_cert(c, held["hk_root_pub"], now_ms=ts, dev_mode=dev_mode)
            if oc["hub_id"] != held["card"]["hub_id"]:
                raise Refused("other_hub")
            return unb64u(oc["hk_op_pub"], 65), oc["op_seq"]
    raise Refused("stale_op_cert")


def verify_record_ex(
    signed: Any,
    held: Mapping[str, Any],
    *,
    dev_mode: bool,
    event: Any = None,
    op_certs: Any = (),
    revoked_op_certs: frozenset[str] = frozenset(),
    trusted_seq: int = 0,
) -> tuple[dict[str, Any], int]:
    """The record under the op cert it names (see :func:`_record_key`), then fields, hub, dev marker and genesis rule.
    If the host has the event (it concerns the host), it must hash to ``event_hash``, be a valid event of the record's
    ``type`` and match its ``scope_tag``: ``audit_mismatch`` otherwise. Returns ``(record object, op_seq)``."""
    o = _envelope(signed, "hub.audit")
    pub, op_seq = _record_key(o, held, op_certs, dev_mode, revoked_op_certs, trusted_seq)
    _signature(signed, L["sig_hub_audit"], pub)
    _fields(o, "hub.audit")
    if o["hub_id"] != held["card"]["hub_id"]:
        raise Refused("other_hub")
    _dev(o, dev_mode)
    if o["seq"] == 1 and o["prev"] != GENESIS_PREV:
        raise Refused("malformed")
    if event is not None:
        if event_hash(event) != o["event_hash"]:
            raise Refused("audit_mismatch")
        check_event(o["type"], event)
        if scope_tag_of(event) != o["scope_tag"]:
            raise Refused("audit_mismatch")
    return o, op_seq


def verify_record(signed: Any, held: Mapping[str, Any], **kw: Any) -> dict[str, Any]:
    return verify_record_ex(signed, held, **kw)[0]


def apply_record(
    head: Mapping[str, Any] | None, signed: Mapping[str, Any], o: Mapping[str, Any], *, op_seq: int | None = None
) -> dict[str, Any]:
    """Chain rule. ``head`` = ``{"seq", "hash", "op_seq"?}`` of the last record the verifier accepted, or None (it
    expects genesis). Next record: ``seq = head.seq + 1`` and ``prev = head.hash``. A skipped ``seq`` is ``audit_gap``;
    the right ``seq`` with another ``prev``, or the head's own ``seq`` with other bytes, is ``audit_fork`` (the hub told
    two stories). A record older than the head is ignored (a host that keeps only its head cannot compare it, and
    overlapping ``audit_get`` pages re-deliver records: not a fork, L2P-10). ``op_seq`` (of the op cert the record
    verified under) never decreases along the chain: ``stale_op_cert``."""
    h = chain_hash(signed)
    if head is None:
        if o["seq"] != 1:
            raise Refused("audit_gap")
        return {"seq": 1, "hash": h, "op_seq": op_seq}
    if o["seq"] < head["seq"]:
        return dict(head)
    if o["seq"] == head["seq"]:
        if h == head["hash"]:
            return dict(head)
        raise Refused("audit_fork")
    if o["seq"] > head["seq"] + 1:
        raise Refused("audit_gap")
    if o["prev"] != head["hash"]:
        raise Refused("audit_fork")
    if op_seq is not None and head.get("op_seq") is not None and op_seq < head["op_seq"]:
        raise Refused("stale_op_cert")
    return {"seq": o["seq"], "hash": h, "op_seq": op_seq if op_seq is not None else head.get("op_seq")}


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
