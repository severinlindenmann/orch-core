"""The workspace side of the hub contract H0 (orch-hub ``docs/protocol.md``), from the text: ids, the hub card and its
op certificate (section 5, 4.1), building and signing ``hub.binding`` / ``hub.unbound`` (9.2, 9.3), the ``hub.joined``
and ``hub.left`` payloads (9.1) and the binding store (9.4). Pure functions; every check fails closed with
:class:`HubRefused`."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from orch.canon.jcs import JcsError
from orch.canon.jcs import dumps as cj
from orch.crypto import suite as _suite
from orch.crypto.encoding import EncodingError, b64u, unb64u, unhex

from .errors import HubRefused
from .marketpolicy import parse_policy

SUITE_ID = _suite.SUITE_ID
HV = 0
CLOCK_SKEW_MS = 300_000
MODES = ("selective", "full")
_CanonError = (JcsError, EncodingError)

SIG_OP_CERT = b"orch/v2/hub-op-cert|"
SIG_CARD = b"orch/v2/hub-card|"
SIG_OP_REVOKED = b"orch/v2/hub-op-revoked|"
SIG_SNAPSHOT = b"orch/v2/hub-snapshot|"
SIG_UK_CERT = b"orch/v2/hub-uk-cert|"
SIG_CHANNEL_CERT = b"orch/v2/hub-channel-cert|"
SIG_BINDING = b"orch/v2/sig/hub-binding|"
SIG_UNBOUND = b"orch/v2/sig/hub-unbound|"
SIG_LINK = b"orch/v2/sig/hub-link|"
SIG_ROUTE = b"orch/v2/hub-route|"
_H_HUB_ID = b"orch/v2/id/hub|"
_H_PERSON_ID = b"orch/v2/id/person|"
_H_CHANNEL_ID = b"orch/v2/id/channel|"
FACTORS = ("se-unlocked", "passkey", "password+totp")

Sign = Callable[[bytes], bytes]


def _key_hash(label: bytes, pub: bytes) -> bytes:
    try:
        _suite.validate_public_key(pub)
    except _suite.CryptoError:
        raise HubRefused("malformed") from None
    return _suite.sha256(label + bytes([SUITE_ID]) + pub)


def hub_id(hk_root_pub: bytes) -> str:
    return _key_hash(_H_HUB_ID, hk_root_pub)[:16].hex()


def person_id(pk_pub: bytes) -> str:
    return _key_hash(_H_PERSON_ID, pk_pub)[:16].hex()


def hex16(x: Any) -> bool:
    try:
        unhex(x, 16)
        return True
    except _CanonError:
        return False


def is_pub(x: Any) -> bool:
    try:
        _suite.validate_public_key(unb64u(x, 65))
        return True
    except (*_CanonError, _suite.CryptoError):
        return False


def _policy_ok(x: Any) -> bool:
    try:
        parse_policy(x)
    except HubRefused:
        return False
    return True


def _b64u_n(n: int) -> Callable[[Any], bool]:
    def ok(x: Any) -> bool:
        try:
            unb64u(x, n)
            return True
        except _CanonError:
            return False

    return ok


def _b64u_any(x: Any) -> bool:
    try:
        unb64u(x)
        return True
    except _CanonError:
        return False


def _int_min(lo: int) -> Callable[[Any], bool]:
    return lambda x: type(x) is int and x >= lo


def _relay_url(x: Any) -> bool:
    return x is None or (type(x) is str and x.startswith(("https://", "http://127.0.0.1", "http://localhost")))


_FIELDS: dict[str, dict[str, Callable[[Any], bool]]] = {
    "hub.op_cert": {
        "hub_id": hex16,
        "cert_id": hex16,
        "op_seq": _int_min(1),
        "hk_op_pub": is_pub,
        "issued_ms": _int_min(0),
        "expires_ms": _int_min(1),
        "dev": lambda x: type(x) is bool,
    },
    "hub.op_revoked": {
        "hub_id": hex16,
        "cert_id": hex16,
        "revoked_ms": _int_min(0),
        "dev": lambda x: type(x) is bool,
    },
    "hub.uk_cert": {
        "hub_id": hex16,
        "op_cert_id": hex16,
        "user_id": hex16,
        "uk_pub": is_pub,
        "issued_ms": _int_min(0),
        "expires_ms": _int_min(1),
        "dev": lambda x: type(x) is bool,
    },
    "hub.channel_cert": {
        "hub_id": hex16,
        "op_cert_id": hex16,
        "channel_id": hex16,
        "user_id": hex16,
        "key_pub": is_pub,
        "factor": lambda x: type(x) is str and x in FACTORS,
        "issued_ms": _int_min(0),
        "expires_ms": _int_min(1),
        "dev": lambda x: type(x) is bool,
    },
    "hub.snapshot": {
        "hub_id": hex16,
        "op_cert_id": hex16,
        "workspace_id": hex16,
        "generation": lambda x: type(x) is int and x == 0,
        "version": _int_min(1),
        "issued_ms": _int_min(0),
        "roles": lambda x: type(x) is dict,
        "revoked_channels": lambda x: type(x) is list,
        "market_policy": _policy_ok,  # D14: required, never null (hub protocol 7.1)
        "dev": lambda x: type(x) is bool,
    },
    "hub.card": {
        "hub_id": hex16,
        "hk_root_pub": is_pub,
        "hxk_pub": is_pub,
        "relay_url": _relay_url,
        "tls_spki_sha256": _b64u_n(32),
        "card_seq": _int_min(1),
        "issued_ms": _int_min(0),
        "dev": lambda x: type(x) is bool,
    },
    "hub.space": {
        "hub_id": hex16,
        "op_cert_id": hex16,
        "space_id": hex16,
        "epoch": _int_min(1),
        "members": lambda x: type(x) is list,
        "sk_commit": _b64u_n(32),
        "issued_ms": _int_min(0),
        "dev": lambda x: type(x) is bool,
    },
    "hub.space_wrap": {
        "hub_id": hex16,
        "op_cert_id": hex16,
        "space_id": hex16,
        "epoch": _int_min(1),
        "workspace_id": hex16,
        "wxk_version": _int_min(1),
        "sealed": lambda x: _b64u_any(x),
        "dev": lambda x: type(x) is bool,
    },
    "space.item": {
        "hub_id": hex16,
        "space_id": hex16,
        "epoch": _int_min(1),
        "members_hash": _b64u_n(32),
        "item_id": hex16,
        "workspace_id": hex16,
        "created_ms": _int_min(0),
        "body": lambda x: type(x) is dict,
        "dev": lambda x: type(x) is bool,
    },
    "space.accept": {
        "hub_id": hex16,
        "space_id": hex16,
        "workspace_id": hex16,
        "epoch": _int_min(1),
        "prev_members_hash": _b64u_n(32),
        "members_hash": _b64u_n(32),
        "dev": lambda x: type(x) is bool,
    },
    "hub.link": {
        "hub_id": hex16,
        "workspace_id": hex16,
        "dir": lambda x: x in ("w2h", "h2w"),
        "seq": _int_min(1),
        "ts_ms": _int_min(0),
        "op": lambda x: type(x) is str,
        "re": lambda x: x is None or _int_min(1)(x),
        "body": lambda x: type(x) is dict,
    },
}


def _envelope(signed: Any, kind: str, extra: tuple[str, ...] = ()) -> dict[str, Any]:
    if type(signed) is not dict or set(signed) != {"o", "sig", *extra}:
        raise HubRefused("malformed")
    o = signed["o"]
    if type(o) is not dict or o.get("kind") != kind:
        raise HubRefused("malformed")
    if type(o.get("suite")) is not int or o["suite"] != SUITE_ID:
        raise HubRefused("wrong_suite")
    if type(o.get("v")) is not int or o["v"] != 2 or type(o.get("hv")) is not int or o["hv"] != HV:
        raise HubRefused("malformed")
    try:
        cj(signed)
    except _CanonError:
        raise HubRefused("malformed") from None
    return o


def _signature(signed: Mapping[str, Any], label: bytes, pub: bytes) -> None:
    try:
        sig = unb64u(signed["sig"], _suite.SIG_LEN)
        msg = label + cj(signed["o"])
    except _CanonError:
        raise HubRefused("malformed") from None
    if not _suite.verify(pub, sig, msg):
        raise HubRefused("bad_signature")


def _fields(o: Mapping[str, Any], kind: str) -> None:
    spec = _FIELDS[kind]
    if set(o) != {"v", "suite", "kind", "hv", *spec} or not all(ok(o[k]) for k, ok in spec.items()):
        raise HubRefused("malformed")


def check_dev(o_dev: bool, dev_mode: bool) -> None:
    """Section 2.4: a real workspace refuses ``dev: true``; a dev workspace refuses ``dev: false``. Only dev
    workspaces join a dev hub. Whether this host is dev is its own state, never a field of what it checks."""
    if o_dev is True and not dev_mode:
        raise HubRefused("dev_refused")
    if o_dev is False and dev_mode:
        raise HubRefused("dev_mismatch")


# --- the hub card -------------------------------------------------------------------------------------------------


def verify_card(
    card: Any,
    *,
    now_ms: int,
    dev_mode: bool,
    pinned_root: bytes | None = None,
    revoked_op_certs: frozenset[str] = frozenset(),
    held: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Section 5 steps 1-6 (the card a host takes from the invite's hub). Returns ``{"card", "op_cert", "hk_op_pub",
    "hk_root_pub", "signed"}``. ``pinned_root`` is the pin the host already holds, if any (``other_hub``).
    ``revoked_op_certs`` is the host's revoked set (``revoked``). ``held`` is the result of an earlier call (the card
    the host holds): the root-signed ``op_seq`` orders op certs first, ``card_seq`` only within one op cert (L2-1;
    ``stale_op_cert``, ``stale_card``)."""
    o = _envelope(card, "hub.card", ("op_cert",))
    if not is_pub(o.get("hk_root_pub")):
        raise HubRefused("malformed")
    root = unb64u(o["hk_root_pub"], 65)
    if pinned_root is not None and root != pinned_root:
        raise HubRefused("other_hub")
    if o.get("hub_id") != hub_id(root):
        raise HubRefused("hub_id_mismatch")
    op = _envelope(card["op_cert"], "hub.op_cert")
    _signature(card["op_cert"], SIG_OP_CERT, root)
    _fields(op, "hub.op_cert")
    if op["hub_id"] != o["hub_id"]:
        raise HubRefused("other_hub")
    check_dev(op["dev"], dev_mode)
    if op["expires_ms"] <= op["issued_ms"] or op["issued_ms"] > now_ms + CLOCK_SKEW_MS:
        raise HubRefused("cert_invalid")
    if now_ms >= op["expires_ms"]:
        raise HubRefused("cert_expired")
    if op["cert_id"] in revoked_op_certs:
        raise HubRefused("revoked")
    hk_op = unb64u(op["hk_op_pub"], 65)
    _signature(card, SIG_CARD, hk_op)
    _fields(o, "hub.card")
    check_dev(o["dev"], dev_mode)
    if o["issued_ms"] > now_ms + CLOCK_SKEW_MS:
        raise HubRefused("cert_invalid")
    if held is not None:
        if root != held["hk_root_pub"]:
            raise HubRefused("other_hub")
        hop = held["op_cert"]
        if op["op_seq"] < hop["op_seq"] or (op["op_seq"] == hop["op_seq"] and op["cert_id"] != hop["cert_id"]):
            raise HubRefused("stale_op_cert")
        if op["op_seq"] == hop["op_seq"]:
            if o["card_seq"] < held["card"]["card_seq"]:
                raise HubRefused("stale_card")
            if o["card_seq"] == held["card"]["card_seq"] and cj(card) != cj(held["signed"]):
                raise HubRefused("stale_card")
    return {"card": o, "op_cert": op, "hk_op_pub": hk_op, "hk_root_pub": root, "signed": card}


def channel_id(key_pub: bytes) -> str:
    return _key_hash(_H_CHANNEL_ID, key_pub)[:16].hex()


def _under_card(
    signed: Any, kind: str, label: bytes, held: Mapping[str, Any], dev_mode: bool, revoked_op_certs: frozenset[str]
) -> dict[str, Any]:
    """Section 6.1: an object signed by ``HK_op``, verified under the op key of a card the verifier already verified
    (``held`` is the result of :func:`verify_card`). Only the card's current op cert counts, and it is checked against
    the CURRENT revoked set at every use (L2-1)."""
    if held["op_cert"]["cert_id"] in revoked_op_certs:
        raise HubRefused("revoked")
    o = _envelope(signed, kind)
    _signature(signed, label, held["hk_op_pub"])
    _fields(o, kind)
    if o["hub_id"] != held["card"]["hub_id"]:
        raise HubRefused("other_hub")
    if o["op_cert_id"] != held["op_cert"]["cert_id"]:
        raise HubRefused("stale_op_cert")
    check_dev(o["dev"], dev_mode)
    return o


def _cert_time(o: Mapping[str, Any], now_ms: int) -> None:
    if o["expires_ms"] <= o["issued_ms"] or o["issued_ms"] > now_ms + CLOCK_SKEW_MS:
        raise HubRefused("cert_invalid")
    if now_ms >= o["expires_ms"]:
        raise HubRefused("cert_expired")


def verify_uk_cert(
    signed: Any, held: Mapping[str, Any], *, now_ms: int, dev_mode: bool, revoked_op_certs: frozenset[str] = frozenset()
) -> dict[str, Any]:
    """Section 6.1: the user's ``UK`` certificate under the held card."""
    o = _under_card(signed, "hub.uk_cert", SIG_UK_CERT, held, dev_mode, revoked_op_certs)
    _cert_time(o, now_ms)
    return o


def verify_channel_cert(
    signed: Any, held: Mapping[str, Any], *, now_ms: int, dev_mode: bool, revoked_op_certs: frozenset[str] = frozenset()
) -> dict[str, Any]:
    """Section 6.2: a channel certificate under the held card; ``channel_id`` is derived from ``key_pub``."""
    o = _under_card(signed, "hub.channel_cert", SIG_CHANNEL_CERT, held, dev_mode, revoked_op_certs)
    if o["channel_id"] != channel_id(unb64u(o["key_pub"], 65)):
        raise HubRefused("cert_invalid")
    _cert_time(o, now_ms)
    return o


def verify_op_revoked(signed: Any, hk_root_pub: bytes) -> dict[str, Any]:
    """Section 4.2. No dev-marker check (L2-8): a revocation only removes authority, so refusing one would fail open."""
    o = _envelope(signed, "hub.op_revoked")
    _signature(signed, SIG_OP_REVOKED, hk_root_pub)
    _fields(o, "hub.op_revoked")
    if o["hub_id"] != hub_id(hk_root_pub):
        raise HubRefused("other_hub")
    return o


# --- binding and unbound (signed by WSK) --------------------------------------------------------------------------


def _head(kind: str) -> dict[str, Any]:
    return {"v": 2, "suite": SUITE_ID, "kind": kind, "hv": HV}


def _signed(o: dict[str, Any], label: bytes, sign: Sign) -> dict[str, Any]:
    return {"o": o, "sig": b64u(sign(label + cj(o)))}


def build_binding(
    *,
    workspace_id: str,
    wsk_pub: bytes,
    hk_root_pub: bytes,
    mode: str,
    binding_seq: int,
    valid_from_ms: int,
    dev: bool,
    sign: Sign,
) -> dict[str, Any]:
    if mode not in MODES or not hex16(workspace_id) or type(binding_seq) is not int or binding_seq < 1:
        raise HubRefused("malformed")
    o = {
        **_head("hub.binding"),
        "workspace_id": workspace_id,
        "wsk_pub": b64u(wsk_pub),
        "hub_id": hub_id(hk_root_pub),
        "hk_root_pub": b64u(hk_root_pub),
        "mode": mode,
        "binding_seq": binding_seq,
        "valid_from_ms": valid_from_ms,
        "dev": dev,
    }
    return _signed(o, SIG_BINDING, sign)


def build_unbound(
    *, workspace_id: str, hub_id_: str, binding_seq: int, unbound_ms: int, dev: bool, sign: Sign
) -> dict[str, Any]:
    if not (hex16(workspace_id) and hex16(hub_id_)) or type(binding_seq) is not int or binding_seq < 1:
        raise HubRefused("malformed")
    o = {
        **_head("hub.unbound"),
        "workspace_id": workspace_id,
        "hub_id": hub_id_,
        "binding_seq": binding_seq,
        "unbound_ms": unbound_ms,
        "dev": dev,
    }
    return _signed(o, SIG_UNBOUND, sign)


def apply_binding(state: Mapping[str, Any] | None, b: Mapping[str, Any]) -> dict[str, Any]:
    """The binding store transition of section 9.4 (a binding never comes back after its leave)."""
    if state is not None:
        if b["binding_seq"] <= state["binding_seq"] and state["status"] == "unbound":
            raise HubRefused("unbound")
        if b["binding_seq"] < state["binding_seq"]:
            raise HubRefused("stale_binding")
        if b["binding_seq"] == state["binding_seq"]:
            if cj(dict(b)) != cj(dict(state["binding"])):
                raise HubRefused("binding_conflict")
            return dict(state)
    return {"binding_seq": b["binding_seq"], "status": "bound", "binding": dict(b)}


def apply_unbound(state: Mapping[str, Any] | None, u: Mapping[str, Any]) -> dict[str, Any]:
    if state is not None and u["binding_seq"] < state["binding_seq"]:
        raise HubRefused("stale_binding")
    if (
        state is not None
        and state["status"] == "bound"
        and u["binding_seq"] == state["binding_seq"]
        and u["hub_id"] != state["binding"]["hub_id"]
    ):
        raise HubRefused("other_hub")
    return {
        "binding_seq": u["binding_seq"],
        "status": "unbound",
        "binding": None if state is None else state["binding"],
    }


# --- the log payloads (section 9.1) -------------------------------------------------------------------------------


def joined_payload(
    *,
    hk_root_pub: bytes,
    mode: str,
    binding_seq: int,
    aliases: list[dict[str, str]],
    prior_aliases: Mapping[str, str] = {},
) -> dict[str, Any]:
    """The exact members of ``hub.joined``; aliases sorted and unique, never re-pointed (``alias_repointed``)."""
    if mode not in MODES or type(binding_seq) is not int or binding_seq < 1 or not is_pub(b64u(hk_root_pub)):
        raise HubRefused("malformed")
    out, users, pids = dict(prior_aliases), {u: p for p, u in prior_aliases.items()}, []
    for a in aliases:
        if (
            type(a) is not dict
            or set(a) != {"person_id", "user_id"}
            or not (hex16(a["person_id"]) and hex16(a["user_id"]))
        ):
            raise HubRefused("malformed")
        pid, uid = a["person_id"], a["user_id"]
        pids.append(pid)
        if out.get(pid, uid) != uid or users.get(uid, pid) != pid:
            raise HubRefused("alias_repointed")
        out[pid], users[uid] = uid, pid
    if pids != sorted(set(pids)):
        raise HubRefused("malformed")
    return {
        "hub_id": hub_id(hk_root_pub),
        "hk_root_pub": b64u(hk_root_pub),
        "mode": mode,
        "binding_seq": binding_seq,
        "aliases": [{"person_id": a["person_id"], "user_id": a["user_id"]} for a in aliases],
    }


def left_payload(*, hub_id_: str, binding_seq: int) -> dict[str, Any]:
    if not hex16(hub_id_) or type(binding_seq) is not int or binding_seq < 1:
        raise HubRefused("malformed")
    return {"hub_id": hub_id_, "binding_seq": binding_seq}
