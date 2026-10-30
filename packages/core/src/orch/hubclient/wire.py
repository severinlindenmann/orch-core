"""The workspace side of the hub contract H0 (``contracts/hub/protocol.md``), from the text: ids, the hub card and its
op certificate (section 5, 4.1) with its signed reader level, building and signing ``hub.binding`` / ``hub.unbound``
(9.2, 9.3), the ``hub.joined`` and ``hub.left`` payloads (9.1), the binding store (9.4) and the owner proof of a hub op
(9.6, #1198 C8). Pure functions; every check fails closed with :class:`HubRefused`."""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from typing import Any

from orch.canon.jcs import JcsError
from orch.canon.jcs import dumps as cj
from orch.crypto import suite as _suite
from orch.crypto.encoding import EncodingError, b64u, unb64u, unhex
from orch.model import remote as _remote

from .errors import HubRefused
from .marketpolicy import parse_policy

SUITE_ID = _suite.SUITE_ID
HV = 0
CLOCK_SKEW_MS = 300_000
MODES = ("selective", "full")
#: section 5: a card's reader level is a uint31 (orch-v2-host §0.8.6 item 9 levels; 1 = understands auth convenience)
READER_MAX = 2**31 - 1
#: section 9.6: the hub ops an owner proof authorises, its longest life, the host request id it names
OWNER_PROOF_OPS = ("hub.join", "hub.mode", "hub.leave")
OWNER_PROOF_MAX_MS = 600_000
REQUEST_ID = re.compile(r"hr_[0-7][0-9A-HJKMNP-TV-Z]{25}")
JOINED_KEYS = frozenset({"hub_id", "hk_root_pub", "mode", "binding_seq", "aliases"})
LEFT_KEYS = frozenset({"hub_id", "binding_seq"})
#: section 9.6 "Readers": the ops whose proof a recorded event may carry
RECORDED_OPS: Mapping[str, tuple[str, ...]] = {
    "hub.joined": ("hub.join", "hub.mode"),
    "hub.left": ("hub.leave",),
    # remote access R2 (#1590, contract 10.2c "The owner proof of a remote step"): their own label and op set
    "remote.changed": ("remote.set",),
    "remote.device_added": ("remote.device_confirm",),
}
#: remote access R2: the ops the remote owner proof authorises (core-only; the hub never sees these proofs)
REMOTE_PROOF_OPS = ("remote.set", "remote.device_confirm")
REMOTE_LEVELS = _remote.LEVELS
REMOTE_PRESENCE_KINDS = _remote.PRESENCE_KINDS
REMOTE_PREMISES = _remote.PREMISES
REMOTE_HUB_MODES = _remote.HUB_MODES
REMOTE_CHANGED_KEYS = frozenset({"on", "level", "until_ms", "premises", "agents_risk", "hub_mode", "remote_v"})
REMOTE_ADDED_KEYS = _remote.ADDED_KEYS
#: contract 10.2c "The device list": ``added_ms < expires_ms <= added_ms + 366 d``
REMOTE_DEVICE_LIFE_MAX_MS = _remote.DEVICE_LIFE_MAX_MS
REMOTE_DEVICES_MAX = _remote.DEVICES_MAX
_REMOTE_DEVICE_ID = re.compile(r"rd_[0-9a-f]{32}")
_REMOTE_CODE_ID = re.compile(r"rc_[0-9a-f]{32}")
_PERSON_REF = re.compile(r"p_[0-9a-f]{32}")
_HEX64 = re.compile(r"[0-9a-f]{64}")
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
SIG_OWNER_PROOF = b"orch/v2/sig/hub-owner-proof|"  # section 9.6: signed by the person key PK, never a device key
#: remote access R2 (10.2c): the same proof object with its OWN label and kind, so a hub proof never stands in for a
#: remote one or the reverse
SIG_REMOTE_OWNER_PROOF = b"orch/v2/sig/remote-owner-proof|"
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
    # prototype (no domain required): a plain-http relay on a LAN / NetBird address is allowed; hardening issue pending
    return x is None or (type(x) is str and x.startswith(("https://", "http://")))


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
        "reader": lambda x: type(x) is int and 0 <= x <= READER_MAX,
        "issued_ms": _int_min(0),
        "dev": lambda x: type(x) is bool,
    },
    "hub.owner_proof": {
        "workspace_id": hex16,
        "person_id": hex16,
        "op": lambda x: type(x) is str and x in OWNER_PROOF_OPS,
        "payload": lambda x: type(x) is dict,
        "request_id": lambda x: type(x) is str and REQUEST_ID.fullmatch(x) is not None,
        "issued_ms": _int_min(0),
        "expires_ms": _int_min(1),
        "dev": lambda x: type(x) is bool,
    },
    "remote.owner_proof": {
        "workspace_id": hex16,
        "person_id": hex16,
        "op": lambda x: type(x) is str and x in REMOTE_PROOF_OPS,
        "payload": lambda x: type(x) is dict,
        "request_id": lambda x: type(x) is str and REQUEST_ID.fullmatch(x) is not None,
        "issued_ms": _int_min(0),
        "expires_ms": _int_min(1),
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
        "body": lambda x: space_item_body_ok(x),
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


def link_signed_bytes(label: bytes, exporter: bytes, o: Mapping[str, Any]) -> bytes:
    """What a ``hub.link`` signature covers (protocol 10.2, R1): ``label || exporter || cj(o)``. ``exporter`` is the
    connection's 32-byte TLS exporter; it is never sent, so a message is valid on one connection only."""
    if type(exporter) is not bytes or len(exporter) != 32:
        raise HubRefused("malformed")
    try:
        return label + exporter + cj(o)
    except _CanonError:
        raise HubRefused("malformed") from None


def _link_signature(signed: Mapping[str, Any], label: bytes, exporter: bytes, pub: bytes) -> None:
    try:
        sig = unb64u(signed["sig"], _suite.SIG_LEN)
    except _CanonError:
        raise HubRefused("malformed") from None
    if not _suite.verify(pub, sig, link_signed_bytes(label, exporter, signed["o"])):
        raise HubRefused("bad_signature")


def _signature(signed: Mapping[str, Any], label: bytes, pub: bytes) -> None:
    try:
        sig = unb64u(signed["sig"], _suite.SIG_LEN)
        msg = label + cj(signed["o"])
    except _CanonError:
        raise HubRefused("malformed") from None
    if not _suite.verify(pub, sig, msg):
        raise HubRefused("bad_signature")


_BY = re.compile(r"^(person|agent):[A-Za-z0-9_-]{1,64}$")


def space_item_body_ok(b: Any) -> bool:
    """The body of a ``space.item`` (protocol 10.4): ``{name, size, data, by}`` and optionally ``restored_from``. ``by``
    is who the writing host says acted (``person:<id>`` | ``agent:<grant id>``); it is REQUIRED, there is no item
    without it. The hub cannot verify it."""
    if type(b) is not dict or not {"name", "size", "data", "by"} <= set(b) <= {
        "name",
        "size",
        "data",
        "by",
        "restored_from",
    }:
        return False
    return (
        type(b["name"]) is str
        and type(b["size"]) is int
        and b["size"] >= 0
        and type(b["data"]) is str
        and type(b["by"]) is str
        and _BY.fullmatch(b["by"]) is not None
        and ("restored_from" not in b or hex16(b["restored_from"]))
    )


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
        if o["reader"] < held["card"]["reader"]:
            raise HubRefused("reader_downgrade")  # #1198 C8: a hub never lowers its signed reader level
    return {"card": o, "op_cert": op, "hk_op_pub": hk_op, "hk_root_pub": root, "signed": card}


def check_hub_reader(held: Mapping[str, Any], required: int) -> int:
    """Section 5 "Reader level" (#1198 C8): the host's gate before ``hub.join`` / ``hub.mode`` binds the workspace.
    ``held`` is a :func:`verify_card` result, ``required`` the workspace's ``required_reader`` (§0.8.6 item 9.4). A card
    below it is ``hub_too_old`` (the host reports ``sign.convenience_readers`` reason ``hub_too_old``)."""
    level = held["card"]["reader"]
    if type(required) is not int or required < 0 or level < required:
        raise HubRefused("hub_too_old")
    return level


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
    owner_proof: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """The exact members of ``hub.joined``; aliases sorted and unique, never re-pointed (``alias_repointed``).
    ``owner_proof`` (#1198 C9) is ``None`` in a prepared event: the signer fills it after the review (like a grant's
    ``secret_hash``); the proof itself is over :func:`without_proof` of this payload."""
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
        "owner_proof": None if owner_proof is None else dict(owner_proof),
    }


def left_payload(*, hub_id_: str, binding_seq: int, owner_proof: Mapping[str, Any] | None = None) -> dict[str, Any]:
    if not hex16(hub_id_) or type(binding_seq) is not int or binding_seq < 1:
        raise HubRefused("malformed")
    return {
        "hub_id": hub_id_,
        "binding_seq": binding_seq,
        "owner_proof": None if owner_proof is None else dict(owner_proof),
    }


def changed_payload(
    *,
    level: str,
    until_ms: int | None,
    premises: str,
    agents_risk: bool,
    hub_mode: str,
    remote_v: int,
    owner_proof: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """The exact members of ``remote.changed`` (R2); ``remote_v`` is the model's remote counter the owner saw.
    ``owner_proof`` is ``None`` in a prepared event: the signer fills it after the review. ``malformed`` for anything
    the model would refuse by shape."""
    p = {
        "on": True, "level": level, "until_ms": until_ms, "premises": premises, "agents_risk": agents_risk,
        "hub_mode": hub_mode, "remote_v": remote_v,
    }  # fmt: skip
    if not _changed_shape(p):
        raise HubRefused("malformed")
    return {**p, "owner_proof": None if owner_proof is None else dict(owner_proof)}


def device_added_payload(
    *,
    device_id: str,
    name: str,
    person: str,
    presence_kind: str,
    transport_pub: str,
    presence_pub: str,
    level_cap: str,
    expires_ms: int,
    code_id: str,
    remote_v: int,
    owner_proof: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """The exact members of ``remote.device_added`` (R2); ``fingerprint`` is recomputed here from the two public keys
    (never taken from the device or the request)."""
    try:
        fp = device_fp(presence_kind, unb64u(transport_pub, 32), unb64u(presence_pub, 65)).hex()
    except _CanonError:
        raise HubRefused("malformed") from None
    p = {
        "device_id": device_id, "name": name, "person": person, "presence_kind": presence_kind,
        "transport_pub": transport_pub, "presence_pub": presence_pub, "level_cap": level_cap,
        "expires_ms": expires_ms, "fingerprint": fp, "code_id": code_id, "remote_v": remote_v,
    }  # fmt: skip
    if not _added_shape(p):
        raise HubRefused("malformed")
    return {**p, "owner_proof": None if owner_proof is None else dict(owner_proof)}


def without_proof(payload: Mapping[str, Any]) -> dict[str, Any]:
    """The payload an owner proof is over: the event's members without ``owner_proof`` (9.6)."""
    return {k: v for k, v in payload.items() if k != "owner_proof"}


# --- the owner proof of a hub op (section 9.6, #1198 C8) ---------------------------------------------------------


def _joined_shape(p: Any) -> bool:
    if not (
        type(p) is dict
        and set(p) == JOINED_KEYS
        and is_pub(p["hk_root_pub"])
        and hex16(p["hub_id"])
        and p["mode"] in MODES
        and type(p["binding_seq"]) is int
        and p["binding_seq"] >= 1
        and type(p["aliases"]) is list
    ):
        return False
    if p["hub_id"] != hub_id(unb64u(p["hk_root_pub"], 65)):
        return False
    pids, uids = [], []
    for a in p["aliases"]:
        if type(a) is not dict or set(a) != {"person_id", "user_id"}:
            return False
        if not (hex16(a["person_id"]) and hex16(a["user_id"])):
            return False
        pids.append(a["person_id"])
        uids.append(a["user_id"])
    return pids == sorted(set(pids)) and len(set(uids)) == len(uids)  # one user id never names two people


def _left_shape(p: Any) -> bool:
    return (
        type(p) is dict
        and set(p) == LEFT_KEYS
        and hex16(p["hub_id"])
        and type(p["binding_seq"]) is int
        and p["binding_seq"] >= 1
    )


def recorded_keys(event_type: str) -> frozenset[str]:
    """The exact members (without ``owner_proof``) of a recorded event that carries an owner proof."""
    return _RECORDED_KEYS[event_type]


def _fingerprint_ok(x: Any) -> bool:
    return type(x) is str and _HEX64.fullmatch(x) is not None


def _changed_shape(p: Any) -> bool:
    """``remote.changed`` without ``owner_proof`` (R2): the owner's policy, always an "on" (off is the host's
    ``remote.disabled``). An end date is absent only when the computer is not at a customer's place and the level is
    below terminals; a computer at a customer's place never shares a hub's full mode with remote access."""
    if not (
        type(p) is dict
        and set(p) == REMOTE_CHANGED_KEYS
        and p["on"] is True
        and type(p["level"]) is str
        and p["level"] in REMOTE_LEVELS
        and (p["until_ms"] is None or (type(p["until_ms"]) is int and p["until_ms"] >= 1))
        and type(p["premises"]) is str
        and p["premises"] in REMOTE_PREMISES
        and type(p["agents_risk"]) is bool
        and type(p["hub_mode"]) is str
        and p["hub_mode"] in REMOTE_HUB_MODES
        and _int_min(0)(p["remote_v"])
    ):
        return False
    if p["until_ms"] is None and (p["premises"] == "yes" or p["level"] == "terminals"):
        return False
    return not (p["premises"] == "yes" and p["hub_mode"] == "full")


def _added_shape(p: Any) -> bool:
    """``remote.device_added`` without ``owner_proof`` (R2): one device the owner confirmed at the workspace."""
    from .custodial_shape import text_ok

    if not (type(p) is dict and set(p) == REMOTE_ADDED_KEYS):
        return False
    return (
        type(p["device_id"]) is str
        and _REMOTE_DEVICE_ID.fullmatch(p["device_id"]) is not None
        and text_ok(p["name"], 64)
        and type(p["person"]) is str
        and _PERSON_REF.fullmatch(p["person"]) is not None
        and type(p["presence_kind"]) is str
        and p["presence_kind"] in REMOTE_PRESENCE_KINDS
        and _b64u_n(32)(p["transport_pub"])
        and is_pub(p["presence_pub"])
        and type(p["level_cap"]) is str
        and p["level_cap"] in REMOTE_LEVELS
        and type(p["expires_ms"]) is int
        and p["expires_ms"] >= 1
        and _fingerprint_ok(p["fingerprint"])
        and _int_min(0)(p["remote_v"])
        and type(p["code_id"]) is str
        and _REMOTE_CODE_ID.fullmatch(p["code_id"]) is not None
    )


def device_fp(presence_kind: str, transport_pub: bytes, presence_pub: bytes) -> bytes:
    """Contract 10.2c (see :func:`orch.model.remote.device_fp`, the one implementation); ``malformed`` on any error."""
    try:
        return _remote.device_fp(presence_kind, transport_pub, presence_pub)
    except ValueError:
        raise HubRefused("malformed") from None


def fingerprint_words(fp: bytes) -> str:
    """Contract 16 row 68 (see :func:`orch.model.remote.fingerprint_words`); ``malformed`` on any error."""
    try:
        return _remote.fingerprint_words(fp)
    except ValueError:
        raise HubRefused("malformed") from None


def _family(op: str) -> tuple[str, bytes]:
    """The proof kind and signature label of an op: hub ops and remote ops never share either."""
    if op in REMOTE_PROOF_OPS:
        return "remote.owner_proof", SIG_REMOTE_OWNER_PROOF
    return "hub.owner_proof", SIG_OWNER_PROOF


def _proof_payload_ok(op: str, p: Any) -> bool:
    """The payload has the exact shape of its event's payload without ``owner_proof``; ``hub.mode`` only to full."""
    if op == "remote.set":
        return _changed_shape(p)
    if op == "remote.device_confirm":
        return _added_shape(p)
    if op == "hub.leave":
        return _left_shape(p)
    return _joined_shape(p) and (op != "hub.mode" or p["mode"] == "full")


def build_owner_proof(
    *,
    workspace_id: str,
    pk_pub: bytes,
    op: str,
    payload: Mapping[str, Any],
    request_id: str,
    issued_ms: int,
    expires_ms: int,
    dev: bool,
    sign: Sign,
) -> dict[str, Any]:
    """Section 9.6: the proof object, signed by ``sign`` (the person key ``PK``, held only for this call by the signer,
    C10). ``payload`` is the event payload WITHOUT its ``owner_proof`` member. Refuses (``malformed``) anything the
    verifier would refuse, so a signer never signs a proof that cannot verify. A remote op (R2) gets the remote kind
    and label."""
    kind, label = _family(op)
    o = {
        **_head(kind),
        "workspace_id": workspace_id,
        "person_id": person_id(pk_pub),
        "op": op,
        "payload": dict(payload),
        "request_id": request_id,
        "issued_ms": issued_ms,
        "expires_ms": expires_ms,
        "dev": dev,
    }
    try:
        _fields(o, kind)
        cj(o)
    except (HubRefused, *_CanonError):
        raise HubRefused("malformed") from None
    if not _proof_payload_ok(op, o["payload"]) or not issued_ms < expires_ms <= issued_ms + OWNER_PROOF_MAX_MS:
        raise HubRefused("malformed")
    return _signed(o, label, sign)


def _owner_proof(signed: Any, pk_pub: Any, family: str = "hub.owner_proof") -> dict[str, Any]:
    """Section 9.6 steps 1-3: one code, ``proof_invalid``, for every way the object itself is wrong. ``family`` is the
    kind the caller expects (``hub.owner_proof`` or ``remote.owner_proof``): the other family's object is wrong."""
    label = SIG_REMOTE_OWNER_PROOF if family == "remote.owner_proof" else SIG_OWNER_PROOF
    try:
        if type(pk_pub) is not bytes:
            raise HubRefused("malformed")
        _suite.validate_public_key(pk_pub)
        o = _envelope(signed, family)
        _signature(signed, label, pk_pub)
        _fields(o, family)
    except (HubRefused, _suite.CryptoError):
        raise HubRefused("proof_invalid") from None
    if o["person_id"] != person_id(pk_pub) or not _proof_payload_ok(o["op"], o["payload"]):
        raise HubRefused("proof_invalid")
    if not o["issued_ms"] < o["expires_ms"] <= o["issued_ms"] + OWNER_PROOF_MAX_MS:
        raise HubRefused("proof_invalid")
    return o


def _same(a: Any, b: Any) -> bool:
    try:
        return cj(a) == cj(b)
    except _CanonError:
        return False


def check_owner_proof(
    signed: Any,
    *,
    pk_pub: bytes,
    workspace_id: str,
    op: str,
    payload: Mapping[str, Any],
    request_id: str,
    now_ms: int,
    dev_mode: bool,
) -> dict[str, Any]:
    """Section 9.6 "At the host" (C9 calls it at binding and again at submit, under the store lock): ``pk_pub`` the
    acting owner's ``PK`` from the roster, ``payload`` the event payload without ``owner_proof``, ``request_id`` the
    request being bound or submitted. Every argument is required: nothing is skipped."""
    o = _owner_proof(signed, pk_pub, _family(op)[0])
    if o["workspace_id"] != workspace_id or o["op"] != op or o["request_id"] != request_id:
        raise HubRefused("proof_invalid")
    if not _same(o["payload"], dict(payload)):
        raise HubRefused("proof_invalid")
    if o["issued_ms"] > now_ms + CLOCK_SKEW_MS:
        raise HubRefused("proof_invalid")
    if now_ms >= o["expires_ms"]:
        raise HubRefused("proof_expired")
    check_dev(o["dev"], dev_mode)
    return o


PRIOR_TYPES = ("hub.joined", "hub.left")
_RECORDED_KEYS: Mapping[str, frozenset[str]] = {
    "hub.joined": JOINED_KEYS,
    "hub.left": LEFT_KEYS,
    "remote.changed": REMOTE_CHANGED_KEYS,
    "remote.device_added": REMOTE_ADDED_KEYS,
}


def _placement(op: str, payload: Mapping[str, Any], prior: Any) -> None:
    """Section 9.6 "Readers" step 2: from the reader's own record of the last ``hub.joined`` / ``hub.left``
    (``prior``: ``None`` or ``{type, hub_id, mode, binding_seq}``, ``mode`` ``None`` after a leave). A join follows
    nothing or a leave; a mode switch a selective join of the same hub; a leave a join of the same hub."""
    if prior is not None and not (
        type(prior) is dict
        and set(prior) == {"type", "hub_id", "mode", "binding_seq"}
        and prior["type"] in PRIOR_TYPES
        and hex16(prior["hub_id"])
        and type(prior["binding_seq"]) is int
        and prior["binding_seq"] >= 1
        and (prior["mode"] in MODES if prior["type"] == "hub.joined" else prior["mode"] is None)
    ):
        raise HubRefused("malformed")
    last = 0 if prior is None else prior["binding_seq"]
    joined = prior is not None and prior["type"] == "hub.joined"
    same_hub = prior is not None and prior["hub_id"] == payload["hub_id"]
    if op == "hub.join":
        ok = not joined and payload["binding_seq"] == last + 1
    elif op == "hub.mode":
        ok = joined and same_hub and prior["mode"] == "selective" and payload["binding_seq"] == last + 1
    else:
        ok = joined and same_hub and payload["binding_seq"] == last
    if not ok:
        raise HubRefused("stale_binding")


def check_recorded_owner_proof(
    event_type: str,
    payload: Mapping[str, Any],
    *,
    at_ms: Any,
    prior: Any,
    workspace_id: str,
    actor_person_id: str,
    pk_pub: bytes | None,
    dev_mode: bool,
) -> dict[str, Any]:
    """Section 9.6 "Readers": every ``hub.joined`` / ``hub.left`` carries a valid owner proof (``null`` is
    ``proof_missing``, whatever the ``auth``). The deciding inputs are the reader's own facts: ``pk_pub`` (the actor's
    ``PK`` from the roster, ``None``: refused), ``prior`` (see :func:`_placement`) and ``at_ms``, the append time of the
    host's chained, host-signed log line (never the event body or a deliverer's value). Steps: the proof and its
    bindings, the place (``stale_binding``), the time inside the proof's life (``proof_expired``), the dev marker."""
    allowed = RECORDED_OPS.get(event_type)
    keys = _RECORDED_KEYS.get(event_type)
    if allowed is None or keys is None or type(at_ms) is not int or at_ms < 0:
        raise HubRefused("malformed")
    if type(payload) is not dict or set(payload) != {*keys, "owner_proof"}:
        raise HubRefused("malformed")
    proof = payload["owner_proof"]
    if proof is None:
        raise HubRefused("proof_missing")
    if type(proof) is not dict:
        raise HubRefused("malformed")
    if pk_pub is None:
        raise HubRefused("proof_invalid")
    o = _owner_proof(proof, pk_pub, _family(allowed[0])[0])
    rest = {k: v for k, v in payload.items() if k != "owner_proof"}
    if o["workspace_id"] != workspace_id or o["person_id"] != actor_person_id or o["op"] not in allowed:
        raise HubRefused("proof_invalid")
    if not _same(o["payload"], rest):
        raise HubRefused("proof_invalid")
    if event_type in PRIOR_TYPES:  # a hub step has a place in the binding; a remote step has none (R2)
        _placement(o["op"], rest, prior)
    if not o["issued_ms"] - CLOCK_SKEW_MS <= at_ms <= o["expires_ms"]:
        raise HubRefused("proof_expired")
    check_dev(o["dev"], dev_mode)
    return o
