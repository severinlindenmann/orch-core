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
    CLOCK_SKEW_MS,
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
#: own ``nonce`` (L2P-4): only the workspace the event is about receives the event and can check the tag; every
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
    # D14 (§7.1): the hub-wide marketplace policy was replaced; hub-wide, so no scope tag. The version it produced and its hash.
    "market_policy": {"version": _int_min(1), "policy_hash": _b64u_n(32), "actor_user_id": _hex16},
    # p4 (§10.2b): a selective workspace shared, replaced or revoked an item, or the hub purged its items at unbind or a
    # mode switch (scoped to that workspace). Per-change field rules in _share_rule.
    "share": {
        "workspace_id": _hex16,
        "item_id": _opt_hex16,
        "change": _enum("put", "revoke", "purge"),
        "item_version": lambda x: x is None or _int_min(1)(x),
        "item_hash": lambda x: x is None or (type(x) is str and len(x) == 71 and x.startswith("sha256:")),
        "count": lambda x: x is None or _int_min(0)(x),
    },
    # p4 (§10.5): a channel key was registered, revoked (permanent) or its certificate renewed for the same key (renew); hub-wide, so no scope tag.
    # actor_user_id: the user who registered or revoked it, or None for the operator at the hub terminal.
    "channel": {
        "channel_id": _hex16,
        "user_id": _hex16,
        "change": _enum("register", "revoke", "renew"),
        "factor": _enum("se-unlocked", "passkey", "password+totp"),
        "actor_user_id": _opt_hex16,
    },
}


def _with_nonce(spec: dict[str, Any]) -> dict[str, Any]:
    return {**spec, "nonce": _b64u_n(32)}


for _t in list(EVENT_FIELDS):  # ONE rule, no type exempt: every event carries 32 random bytes inside it
    EVENT_FIELDS[_t] = _with_nonce(EVENT_FIELDS[_t])


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


def _share_rule(e: Mapping[str, Any]) -> bool:
    """put: item, version, hash, no count; revoke: item, the tombstoned version, hash null, no count (a revoke of an
    unknown or revoked id is the idempotent {} and writes no record); purge: one record per workspace at unbind or a
    switch to full, item, version and hash null, ``count`` = the live items purged (L2 p4, D9)."""
    c, item, ver, h, n = e["change"], e["item_id"], e["item_version"], e["item_hash"], e["count"]
    if c == "put":
        return item is not None and ver is not None and h is not None and n is None
    if c == "revoke":
        return item is not None and ver is not None and h is None and n is None
    return item is None and ver is None and h is None and n is not None


EVENT_RULES: dict[str, Any] = {"share": _share_rule}


def check_event(typ: str, event: Any) -> dict[str, Any]:
    spec = EVENT_FIELDS.get(typ)
    if spec is None or type(event) is not dict or set(event) != set(spec):
        raise Refused("malformed")
    if not all(ok(event[k]) for k, ok in spec.items()):
        raise Refused("malformed")
    rule = EVENT_RULES.get(typ)
    if rule is not None and not rule(event):
        raise Refused("malformed")
    return event


def scope_of(event: Mapping[str, Any]) -> str | None:
    return event.get("workspace_id")


def scope_tag_of(event: Mapping[str, Any]) -> str | None:
    """``b64u(H("orch/v2/hub-audit-scope|" || workspace_id (16) || nonce (32)))``, or None for a hub-wide event.
    The nonce is inside the event, which only the scope's workspace receives, so a co-tenant cannot test a guessed
    ``workspace_id`` against the tag."""
    ws = scope_of(event)
    if ws is None:
        return None
    try:
        return b64u(S.sha256(L["h_hub_audit_scope"] + unhex(ws, 16) + unb64u(event["nonce"], 32)))
    except CanonError, KeyError:
        raise Refused("malformed") from None


def _record_key(
    o: Mapping[str, Any],
    held: Mapping[str, Any],
    op_certs: Any,
    dev_mode: bool,
    now_ms: int,
    trusted_seq: int,
) -> tuple[bytes, int]:
    """The key a record verifies under and that op cert's ``op_seq`` (L2P-3). A record names the op cert it was signed
    under: the held card's current one, or an earlier one that the hub's root certified (``op_certs``, served with
    ``audit_get``) and that was valid at the record's time. A non-current cert must have been valid
    at the record's date AND not have lapsed before its successor was issued (``stale_op_cert``), and, for a record
    beyond the verifier's trusted head (``seq > trusted_seq``), must not be expired at the verifier's own time
    (``cert_expired``): an expired old key cannot be made to sign a "backdated" new record. Records up to the trusted
    head are history already accepted, covered by the hash chain. (Revocation is handled by the caller.)"""
    cid, ts = o.get("op_cert_id"), o.get("ts_ms")
    if not _hex16(cid) or type(o.get("seq")) is not int or type(ts) is not int:
        raise Refused("malformed")
    if ts > now_ms + CLOCK_SKEW_MS:
        raise Refused("not_yet_valid")  # a record from the future (L2P-3: no dating by the record alone)
    cur = held["op_cert"]
    if cid == cur["cert_id"]:
        return held["hk_op_pub"], cur["op_seq"]
    for c in op_certs or ():
        if type(c) is dict and type(c.get("o")) is dict and c["o"].get("cert_id") == cid:
            oc = verify_op_cert(c, held["hk_root_pub"], now_ms=ts, dev_mode=dev_mode)
            if oc["hub_id"] != held["card"]["hub_id"]:
                raise Refused("other_hub")
            if oc["op_seq"] >= cur["op_seq"]:
                raise Refused("stale_op_cert")
            succ = [cur["issued_ms"]]
            for x in op_certs:
                if type(x) is dict and type(x.get("o")) is dict and x["o"].get("op_seq", 0) > oc["op_seq"]:
                    xo = verify_op_cert(x, held["hk_root_pub"], now_ms=x["o"]["issued_ms"], dev_mode=dev_mode)
                    succ.append(xo["issued_ms"])
            if oc["expires_ms"] < min(succ):
                raise Refused("stale_op_cert")  # lapsed before its successor existed
            if o["seq"] > trusted_seq and oc["expires_ms"] <= now_ms:
                raise Refused("cert_expired")
            return unb64u(oc["hk_op_pub"], 65), oc["op_seq"]
    raise Refused("stale_op_cert")


def verify_record_ex(
    signed: Any,
    held: Mapping[str, Any],
    *,
    dev_mode: bool,
    now_ms: int,
    event: Any = None,
    op_certs: Any = (),
    revoked_op_certs: frozenset[str] = frozenset(),
    accepted_hashes: Mapping[int, str] | None = None,
    trusted_seq: int = 0,
) -> tuple[dict[str, Any], int]:
    """The record under the op cert it names (see :func:`_record_key`), then fields, hub, dev marker and genesis rule.
    ``now_ms`` is the verifier's clock (a record may not be dated ahead of it, plus skew). ``accepted_hashes`` maps
    ``seq`` to the chain hash the verifier already accepted: a record signed under a **revoked** op cert verifies only
    if it IS that record (same bytes, same hash), so a revoked key cannot forge anything, not even at a past ``seq``.
    If the host has the event (it concerns the host), it must hash to ``event_hash``, be a valid event of the record's
    ``type`` and match its ``scope_tag``: ``audit_mismatch`` otherwise. Returns ``(record object, op_seq)``."""
    o = _envelope(signed, "hub.audit")
    revoked = o.get("op_cert_id") in revoked_op_certs
    same = (
        revoked
        and accepted_hashes is not None
        and type(o.get("seq")) is int
        and accepted_hashes.get(o["seq"]) == chain_hash(signed)
    )
    if revoked and not same:
        raise Refused("revoked")
    pub, op_seq = _record_key(o, held, op_certs, dev_mode, now_ms, trusted_seq)
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
    head: Mapping[str, Any] | None,
    signed: Mapping[str, Any],
    o: Mapping[str, Any],
    *,
    op_seq: int | None = None,
    current_op_seq: int | None = None,
    accepted_hashes: Mapping[int, str] | None = None,
) -> dict[str, Any]:
    """Chain rule. ``head`` = ``{"seq", "hash", "op_seq"?, "ts_ms"?}`` of the last record the verifier accepted, or None
    (it expects genesis). Next record: ``seq = head.seq + 1`` and ``prev = head.hash``. A skipped ``seq`` is
    ``audit_gap``; the right ``seq`` with another ``prev``, or the head's own ``seq`` with other bytes, is
    ``audit_fork`` (the hub told two stories). A record older than the head is ignored (overlapping ``audit_get`` pages
    re-deliver records, L2P-10) unless the verifier kept the hash it accepted for that ``seq`` (``accepted_hashes``): then
    other bytes are ``audit_fork``. Along the chain ``ts_ms`` never decreases (``stale_timestamp``) and ``op_seq`` of the
    op cert never decreases (``stale_op_cert``): the floor is the head's ``op_seq``, or ``current_op_seq`` (the held
    card's) when the head has none, so a head without it cannot be used to take an old key back."""
    h = chain_hash(signed)
    if head is None:
        if o["seq"] != 1:
            raise Refused("audit_gap")
        return {"seq": 1, "hash": h, "op_seq": op_seq, "ts_ms": o["ts_ms"]}
    if o["seq"] < head["seq"]:
        if accepted_hashes is not None and o["seq"] in accepted_hashes and accepted_hashes[o["seq"]] != h:
            raise Refused("audit_fork")
        return dict(head)
    if o["seq"] == head["seq"]:
        if h == head["hash"]:
            return dict(head)
        raise Refused("audit_fork")
    if o["seq"] > head["seq"] + 1:
        raise Refused("audit_gap")
    if o["prev"] != head["hash"]:
        raise Refused("audit_fork")
    floor = head.get("op_seq") if head.get("op_seq") is not None else current_op_seq
    if op_seq is not None and floor is not None and op_seq < floor:
        raise Refused("stale_op_cert")
    if head.get("ts_ms") is not None and o["ts_ms"] < head["ts_ms"]:
        raise Refused("stale_timestamp")
    return {"seq": o["seq"], "hash": h, "op_seq": op_seq if op_seq is not None else floor, "ts_ms": o["ts_ms"]}


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
