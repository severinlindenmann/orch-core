"""Reference builders and verifiers for the hub contract H0 (contracts/hub/protocol.md). Test support and the hub's own
checks; pure functions, no I/O, no clock (``now_ms`` is always passed in), no key storage.

Every verifier raises :class:`Refused` with one code from contracts/hub/protocol.md §14 and fails closed: any input it
cannot read is ``malformed``. Signed objects follow protocol-v2 §2.4: ``{"o": o, "sig": b64u}``,
``sig = Sign(key, label || cj(o))``; the verifier checks the envelope and ``v``/``suite``/``kind``/``hv`` first, then
the signature, then the exact field set, then the semantics.
"""

from __future__ import annotations

import re
import struct
from collections.abc import Callable, Mapping
from typing import Any

from . import suite as S
from .canon import CanonError, b64u, cj, loads_strict, unb64u, unhex
from .labels import EVENT_TYPE, L, custodial_label
from .market_policy import valid_policy
from .org_bundles import valid_org_mcp, valid_org_skills

HV = 0  # hub contract version (H0)
CONTRACT_NAME = "orch.hub/0"
CLOCK_SKEW_MS = 300_000  # protocol-v2 §15: created/issued may be up to 300 s in the future
LINK_WINDOW_MS = 120_000
PUBLISH_WINDOW_MS = 120_000
NONCE_RETENTION_MS = 240_000
ROLES = ("read", "edit", "admin")
FACTORS = ("se-unlocked", "passkey", "password+totp")
MODES = ("selective", "full")
#: §5: the reader level a hub declares on its card (orch-v2-host §0.8.6 item 9: level 1 understands ``auth:
#: "convenience"`` on every event it reads and refuses an unknown ``auth``). This hub is level 1.
HUB_READER = 1
READER_MAX = 2**31 - 1
#: §9.6: the hub ops an owner proof authorises, the longest a proof may live, and the host request id it names
OWNER_PROOF_OPS = ("hub.join", "hub.mode", "hub.leave")
OWNER_PROOF_MAX_MS = 600_000
REQUEST_ID = re.compile(r"hr_[0-7][0-9A-HJKMNP-TV-Z]{25}")
JOINED_KEYS = frozenset({"hub_id", "hk_root_pub", "mode", "binding_seq", "aliases"})
LEFT_KEYS = frozenset({"hub_id", "binding_seq"})


class Refused(Exception):
    def __init__(self, code: str, **extra: Any) -> None:
        super().__init__(code)
        self.code = code
        self.extra = extra


# --- ids ----------------------------------------------------------------------------------------------------------


def _key_hash(label: str, pub: bytes) -> bytes:
    if not S.valid_public_key(pub):
        raise Refused("malformed")
    return S.sha256(L[label] + bytes([S.SUITE_ID]) + pub)


def hub_id(hk_root_pub: bytes) -> str:
    return _key_hash("h_hub_id", hk_root_pub)[:16].hex()


def channel_id(key_pub: bytes) -> str:
    return _key_hash("h_channel_id", key_pub)[:16].hex()


def person_id(pk_pub: bytes) -> str:
    return _key_hash("h_person_id", pk_pub)[:16].hex()


def device_id(dk_sig_pub: bytes) -> str:
    return _key_hash("h_device_id", dk_sig_pub)[:16].hex()


# --- field checks ---------------------------------------------------------------------------------------------------

Check = Callable[[Any], bool]


_BY = re.compile(r"^(person|agent):[A-Za-z0-9_-]{1,64}$")


def space_item_body_ok(b: Any) -> bool:
    """Mirror of orch.hubclient.wire.space_item_body_ok: ``{name, size, data, by}``, ``by`` required, optional
    ``restored_from`` (an item id)."""
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
        and ("restored_from" not in b or _hex16(b["restored_from"]))
    )


def _hex16(x: Any) -> bool:
    try:
        unhex(x, 16)
        return True
    except CanonError:
        return False


def _pub(x: Any) -> bool:
    try:
        return S.valid_public_key(unb64u(x, 65))
    except CanonError:
        return False


def _b64u_n(n: int) -> Check:
    def ok(x: Any) -> bool:
        try:
            unb64u(x, n)
            return True
        except CanonError:
            return False

    return ok


def _b64u_any(x: Any) -> bool:
    try:
        unb64u(x)
        return True
    except CanonError:
        return False


def _int_min(lo: int) -> Check:
    return lambda x: type(x) is int and x >= lo


def _enum(*vals: Any) -> Check:
    return lambda x: type(x) is str and x in vals


def _bool(x: Any) -> bool:
    return type(x) is bool


def _const(v: Any) -> Check:
    return lambda x: type(x) is type(v) and x == v


_URL = re.compile(
    r"https://[a-z0-9.-]{1,253}(:[0-9]{1,5})?|http://(localhost|127\.0\.0\.1|\[::1\]|10\.(?:25[0-5]|2[0-4][0-9]|1[0-9]{2}|[1-9]?[0-9])\.(?:25[0-5]|2[0-4][0-9]|1[0-9]{2}|[1-9]?[0-9])\.(?:25[0-5]|2[0-4][0-9]|1[0-9]{2}|[1-9]?[0-9])|172\.(?:1[6-9]|2[0-9]|3[01])\.(?:25[0-5]|2[0-4][0-9]|1[0-9]{2}|[1-9]?[0-9])\.(?:25[0-5]|2[0-4][0-9]|1[0-9]{2}|[1-9]?[0-9])|192\.168\.(?:25[0-5]|2[0-4][0-9]|1[0-9]{2}|[1-9]?[0-9])\.(?:25[0-5]|2[0-4][0-9]|1[0-9]{2}|[1-9]?[0-9])|100\.(?:6[4-9]|[7-9][0-9]|1[01][0-9]|12[0-7])\.(?:25[0-5]|2[0-4][0-9]|1[0-9]{2}|[1-9]?[0-9])\.(?:25[0-5]|2[0-4][0-9]|1[0-9]{2}|[1-9]?[0-9]))(:[0-9]{1,5})?"
)


def _relay_url(x: Any) -> bool:
    return x is None or (type(x) is str and _URL.fullmatch(x) is not None)


HEADER = {"v": _const(2), "suite": _const(S.SUITE_ID), "kind": None, "hv": _const(HV)}

FIELDS: dict[str, dict[str, Check]] = {
    "hub.op_cert": {
        "hub_id": _hex16,
        "cert_id": _hex16,
        "op_seq": _int_min(1),
        "hk_op_pub": _pub,
        "issued_ms": _int_min(0),
        "expires_ms": _int_min(1),
        "dev": _bool,
    },
    "hub.op_revoked": {"hub_id": _hex16, "cert_id": _hex16, "revoked_ms": _int_min(0), "dev": _bool},
    "hub.card": {
        "hub_id": _hex16,
        "hk_root_pub": _pub,
        "hxk_pub": _pub,
        "relay_url": _relay_url,
        "tls_spki_sha256": _b64u_n(32),
        "card_seq": _int_min(1),
        "reader": lambda x: type(x) is int and 0 <= x <= READER_MAX,
        "issued_ms": _int_min(0),
        "dev": _bool,
    },
    "hub.uk_cert": {
        "hub_id": _hex16,
        "op_cert_id": _hex16,
        "user_id": _hex16,
        "uk_pub": _pub,
        "issued_ms": _int_min(0),
        "expires_ms": _int_min(1),
        "dev": _bool,
    },
    "hub.channel_cert": {
        "hub_id": _hex16,
        "op_cert_id": _hex16,
        "channel_id": _hex16,
        "user_id": _hex16,
        "key_pub": _pub,
        "factor": _enum(*FACTORS),
        "issued_ms": _int_min(0),
        "expires_ms": _int_min(1),
        "dev": _bool,
    },
    "hub.snapshot": {
        "hub_id": _hex16,
        "op_cert_id": _hex16,
        "workspace_id": _hex16,
        "generation": _const(0),
        "version": _int_min(1),
        "issued_ms": _int_min(0),
        "roles": lambda x: type(x) is dict,
        "revoked_channels": lambda x: type(x) is list,
        "market_policy": valid_policy,  # D14: required, never null (protocol.md §7.1)
        "dev": _bool,
    },
    "hub.org_skills": {  # M3 (#1430): the company skills offered to one workspace; Off until its owner accepts
        "hub_id": _hex16,
        "op_cert_id": _hex16,
        "workspace_id": _hex16,
        "version": _int_min(1),
        "issued_ms": _int_min(0),
        "skills": valid_org_skills,
        "dev": _bool,
    },
    "hub.mcp_servers": {  # M3 (#1435): the remote MCP servers offered to one workspace; Off until its owner accepts
        "hub_id": _hex16,
        "op_cert_id": _hex16,
        "workspace_id": _hex16,
        "version": _int_min(1),
        "issued_ms": _int_min(0),
        "servers": valid_org_mcp,
        "dev": _bool,
    },
    "hub.binding": {
        "workspace_id": _hex16,
        "wsk_pub": _pub,
        "hub_id": _hex16,
        "hk_root_pub": _pub,
        "mode": _enum(*MODES),
        "binding_seq": _int_min(1),
        "valid_from_ms": _int_min(0),
        "dev": _bool,
    },
    "hub.unbound": {
        "workspace_id": _hex16,
        "hub_id": _hex16,
        "binding_seq": _int_min(1),
        "unbound_ms": _int_min(0),
        "dev": _bool,
    },
    "hub.link": {
        "hub_id": _hex16,
        "workspace_id": _hex16,
        "dir": _enum("w2h", "h2w"),
        "seq": _int_min(1),
        "ts_ms": _int_min(0),
        "op": lambda x: type(x) is str,
        "re": lambda x: x is None or _int_min(1)(x),
        "body": lambda x: type(x) is dict,
    },
    "hub.space": {
        "hub_id": _hex16,
        "op_cert_id": _hex16,
        "space_id": _hex16,
        "epoch": _int_min(1),
        "members": lambda x: type(x) is list,
        "sk_commit": _b64u_n(32),
        "issued_ms": _int_min(0),
        "dev": _bool,
    },
    "hub.audit": {
        "hub_id": _hex16,
        "op_cert_id": _hex16,
        "seq": _int_min(1),
        "prev": _b64u_n(32),
        "ts_ms": _int_min(0),
        "type": _enum("custodial", "grant", "membership", "space", "channel", "share", "market_policy"),
        "scope_tag": lambda x: x is None or _b64u_n(32)(x),
        "event_hash": _b64u_n(32),
        "dev": _bool,
    },
    "space.accept": {
        "hub_id": _hex16,
        "space_id": _hex16,
        "workspace_id": _hex16,
        "epoch": _int_min(1),
        "prev_members_hash": _b64u_n(32),
        "members_hash": _b64u_n(32),
        "dev": _bool,
    },
    "hub.space_wrap": {
        "hub_id": _hex16,
        "op_cert_id": _hex16,
        "space_id": _hex16,
        "epoch": _int_min(1),
        "workspace_id": _hex16,
        "wxk_version": _int_min(1),
        "sealed": _b64u_any,
        "dev": _bool,
    },
    "space.item": {
        "hub_id": _hex16,
        "space_id": _hex16,
        "epoch": _int_min(1),
        "members_hash": _b64u_n(32),
        "item_id": _hex16,
        "workspace_id": _hex16,
        "created_ms": _int_min(0),
        "body": lambda x: space_item_body_ok(x),
        "dev": _bool,
    },
    # p4 (§6.3): what a new channel key signs to prove possession at registration (signed by that key)
    "hub.channel_register": {
        "hub_id": _hex16,
        "user_id": _hex16,
        "channel_kind": _enum("phone", "browser"),
        "key_pub": _pub,
        "attestation": _enum("pairing-code", "session"),
        "nonce": _b64u_n(32),
        "expires_ms": _int_min(1),
        "dev": _bool,
    },
    # p4 (B, §6.4, L2B-1): the hub's own public API origin, signed by HK_op; the phone compares it with its link
    "hub.api_origin": {
        "hub_id": _hex16,
        "op_cert_id": _hex16,
        "origin": lambda x: type(x) is str and _URL.fullmatch(x) is not None,
        "issued_ms": _int_min(0),
        "dev": _bool,
    },
    # p4 (B, §6.4): a channel key binds a pairing session of its user to itself (signed by the channel key)
    "hub.channel_auth": {
        "hub_id": _hex16,
        "user_id": _hex16,
        "channel_id": _hex16,
        "session_ref": _b64u_n(32),
        "ts_ms": _int_min(0),
        "dev": _bool,
    },
    # p4 (B, §6.4): a channel key names the push key its hints are sealed to (signed by the channel key)
    "hub.channel_push": {
        "hub_id": _hex16,
        "channel_id": _hex16,
        "push_pub": _pub,
        "issued_ms": _int_min(0),
        "dev": _bool,
    },
    # p4 (§10.2b, C6/D9): one item a selective workspace shares with its hub (signed by its WSK, sealed to HXK)
    "share.item": {
        "hub_id": _hex16,
        "workspace_id": _hex16,
        "item_id": _hex16,
        "item_kind": _enum("needs_you", "ticket", "question"),
        "item_version": _int_min(1),
        "issued_ms": _int_min(0),
        "chain": lambda x: type(x) is dict,
        "content": lambda x: type(x) is dict,
        "dev": _bool,
    },
    # p4 (§10.4, #1020): a proposed member transition of a space, shown to the invited workspace before it accepts
    "hub.space_proposal": {
        "hub_id": _hex16,
        "op_cert_id": _hex16,
        "space_id": _hex16,
        "epoch": _int_min(1),
        "prev_members": lambda x: type(x) is list,
        "members": lambda x: type(x) is list,
        "workspace_id": _hex16,
        "issued_ms": _int_min(0),
        "expires_ms": _int_min(1),
        "dev": _bool,
    },
    # #1198 C8 (§9.6): the owner's proof, signed by PK, that a hub op is the owner's own (not only the device key's)
    "hub.owner_proof": {
        "workspace_id": _hex16,
        "person_id": _hex16,
        "op": _enum(*OWNER_PROOF_OPS),
        "payload": lambda x: type(x) is dict,
        "request_id": lambda x: type(x) is str and REQUEST_ID.fullmatch(x) is not None,
        "issued_ms": _int_min(0),
        "expires_ms": _int_min(1),
        "dev": _bool,
    },
    "hub.publish_approval": {
        "workspace_id": _hex16,
        "hub_id": _hex16,
        "device_id": _hex16,
        "request_hash": _b64u_n(32),
        "access": _enum("public"),
    },
}

LABEL_OF_KIND = {
    "hub.op_cert": "sig_hub_op_cert",
    "hub.op_revoked": "sig_hub_op_revoked",
    "hub.card": "sig_hub_card",
    "hub.uk_cert": "sig_hub_uk_cert",
    "hub.channel_cert": "sig_hub_channel_cert",
    "hub.snapshot": "sig_hub_snapshot",
    "hub.org_skills": "sig_hub_org_skills",
    "hub.mcp_servers": "sig_hub_mcp_servers",
    "hub.binding": "sig_hub_binding",
    "hub.unbound": "sig_hub_unbound",
    "hub.publish_approval": "sig_hub_publish_approval",
    "hub.owner_proof": "sig_hub_owner_proof",
    "hub.space": "sig_hub_space",
    "hub.space_wrap": "sig_hub_space_wrap",
    "hub.audit": "sig_hub_audit",
    "space.item": "sig_space_item",
    "space.accept": "sig_space_accept",
    "hub.channel_register": "sig_hub_channel_register",
    "hub.channel_auth": "sig_hub_channel_auth",
    "hub.api_origin": "sig_hub_api_origin",
    "hub.channel_push": "sig_hub_channel_push",
    "share.item": "sig_share_item",
    "hub.space_proposal": "sig_hub_space_proposal",
    # hub.link: the label depends on the direction (sig_hub_link w2h, sig_hub_route h2w)
}


# --- generic signed objects ----------------------------------------------------------------------------------------


def signing_bytes(label: bytes, o: Mapping[str, Any]) -> bytes:
    try:
        return label + cj(o)
    except CanonError:
        raise Refused("malformed") from None


def _envelope(signed: Any, kind: str, extra_keys: tuple[str, ...] = ()) -> dict[str, Any]:
    """Envelope and header (protocol-v2 §2.4 step 1-2)."""
    if type(signed) is not dict or set(signed) != {"o", "sig", *extra_keys}:
        raise Refused("malformed")
    o = signed["o"]
    if type(o) is not dict or o.get("kind") != kind:
        raise Refused("malformed")
    if o.get("suite") != S.SUITE_ID or type(o.get("suite")) is not int:
        raise Refused("wrong_suite")
    if not (HEADER["v"](o.get("v")) and HEADER["hv"](o.get("hv"))):
        raise Refused("malformed")
    try:
        cj(signed)
    except CanonError:
        raise Refused("malformed") from None
    return o


def _signature(signed: dict[str, Any], label: bytes, pub: bytes) -> None:
    try:
        sig = unb64u(signed["sig"], S.SIG_LEN)
    except CanonError:
        raise Refused("malformed") from None
    if not S.verify(pub, sig, signing_bytes(label, signed["o"])):
        raise Refused("bad_signature")


def _fields(o: dict[str, Any], kind: str) -> None:
    spec = FIELDS[kind]
    if set(o) != {"v", "suite", "kind", "hv", *spec}:
        raise Refused("malformed")
    for name, ok in spec.items():
        if not ok(o[name]):
            raise Refused("malformed")


def _key_field(o: dict[str, Any], name: str) -> bytes:
    """Read one key field before the signature check (it names the verifying key); malformed if it is not a key."""
    v = o.get(name)
    if not _pub(v):
        raise Refused("malformed")
    return unb64u(v, 65)


def _dev(o: dict[str, Any], dev_mode: bool) -> None:
    """The dev marker (spec §6 "Dev hubs"): a real verifier refuses ``dev: true``; a dev verifier (V3) refuses
    ``dev: false``. ``dev`` is a required field, so a missing marker is already ``malformed``."""
    if o["dev"] is True and not dev_mode:
        raise Refused("dev_refused")
    if o["dev"] is False and dev_mode:
        raise Refused("dev_mismatch")


def _cert_time(o: dict[str, Any], now_ms: int, issued: str = "issued_ms") -> None:
    if o["expires_ms"] <= o[issued] or o[issued] > now_ms + CLOCK_SKEW_MS:
        raise Refused("cert_invalid")
    if now_ms >= o["expires_ms"]:
        raise Refused("cert_expired")


def parse(text: bytes | str) -> Any:
    """Strict parse of a received JSON text; anything non-canonical in the subset sense is ``malformed``."""
    try:
        return loads_strict(text)
    except CanonError:
        raise Refused("malformed") from None


# --- HK_root: op certificate and its revocation -------------------------------------------------------------------


def verify_op_cert(
    signed: Any, hk_root_pub: bytes, *, now_ms: int, dev_mode: bool, revoked_op_certs: frozenset[str] = frozenset()
) -> dict[str, Any]:
    o = _envelope(signed, "hub.op_cert")
    _signature(signed, L["sig_hub_op_cert"], hk_root_pub)
    _fields(o, "hub.op_cert")
    if o["hub_id"] != hub_id(hk_root_pub):
        raise Refused("other_hub")
    _dev(o, dev_mode)
    _cert_time(o, now_ms)
    if o["cert_id"] in revoked_op_certs:
        raise Refused("revoked")
    return o


def verify_op_revoked(signed: Any, hk_root_pub: bytes, *, dev_mode: bool) -> dict[str, Any]:
    """No dev-marker check (L2-8): a revocation only removes authority, so refusing one would fail open."""
    o = _envelope(signed, "hub.op_revoked")
    _signature(signed, L["sig_hub_op_revoked"], hk_root_pub)
    _fields(o, "hub.op_revoked")
    if o["hub_id"] != hub_id(hk_root_pub):
        raise Refused("other_hub")
    return o


# --- HK_op: the hub card --------------------------------------------------------------------------------------------


def verify_card(
    card: Any,
    *,
    now_ms: int,
    dev_mode: bool,
    pinned_root: bytes | None = None,
    revoked_op_certs: frozenset[str] = frozenset(),
    held: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Returns ``{"card": o, "op_cert": op_o, "hk_op_pub": bytes, "hk_root_pub": bytes}``.

    ``held`` is the card the verifier holds (the result of an earlier call), if any. Order between cards (L2-1): the
    root-signed ``op_seq`` first, ``card_seq`` only within one op cert. A card under a lower ``op_seq``, or under
    another op cert with the same ``op_seq``, is ``stale_op_cert`` whatever its ``card_seq``; accepting ``op_seq`` N
    ends every op cert below N. A lower ``card_seq``, or the same with other content, is ``stale_card``."""
    o = _envelope(card, "hub.card", ("op_cert",))
    root = _key_field(o, "hk_root_pub")
    if pinned_root is not None and root != pinned_root:
        raise Refused("other_hub")
    if o.get("hub_id") != hub_id(root):
        raise Refused("hub_id_mismatch")
    op = verify_op_cert(card["op_cert"], root, now_ms=now_ms, dev_mode=dev_mode, revoked_op_certs=revoked_op_certs)
    hk_op = unb64u(op["hk_op_pub"], 65)
    _signature(card, L["sig_hub_card"], hk_op)
    _fields(o, "hub.card")
    _dev(o, dev_mode)
    if o["issued_ms"] > now_ms + CLOCK_SKEW_MS:
        raise Refused("cert_invalid")
    if held is not None:
        if root != held["hk_root_pub"]:
            raise Refused("other_hub")
        hop = held["op_cert"]
        if op["op_seq"] < hop["op_seq"] or (op["op_seq"] == hop["op_seq"] and op["cert_id"] != hop["cert_id"]):
            raise Refused("stale_op_cert")
        if op["op_seq"] == hop["op_seq"]:
            if o["card_seq"] < held["card"]["card_seq"]:
                raise Refused("stale_card")
            if o["card_seq"] == held["card"]["card_seq"] and cj(card) != cj(held["signed"]):
                raise Refused("stale_card")
        if o["reader"] < held["card"]["reader"]:
            raise Refused("reader_downgrade")
    return {"card": o, "op_cert": op, "hk_op_pub": hk_op, "hk_root_pub": root, "signed": card}


def check_hub_reader(held: Mapping[str, Any], required: int) -> int:
    """§5 "Reader level" (#1198 C8): the host's gate before it binds the workspace to a hub. ``held`` is a verified
    card (:func:`verify_card`), ``required`` the workspace's ``required_reader`` (orch-v2-host §0.8.6 item 9.4: 1 while
    One click is on or the history holds a convenience event, else 0). A card below it is ``hub_too_old``."""
    level = held["card"]["reader"]
    if type(required) is not int or required < 0 or level < required:
        raise Refused("hub_too_old")
    return level


def _under_card(
    signed: Any, kind: str, held: Mapping[str, Any], dev_mode: bool, revoked_op_certs: frozenset[str]
) -> dict[str, Any]:
    """Objects signed by HK_op: verified under the op key of a card the verifier already verified (``held`` is the
    result of :func:`verify_card`). H0: only the card's current op certificate is accepted. The held card's op cert is
    checked against the CURRENT revoked set at every use (L2-1), not only when the card was admitted."""
    if held["op_cert"]["cert_id"] in revoked_op_certs:
        raise Refused("revoked")
    o = _envelope(signed, kind)
    _signature(signed, L[LABEL_OF_KIND[kind]], held["hk_op_pub"])
    _fields(o, kind)
    if o["hub_id"] != held["card"]["hub_id"]:
        raise Refused("other_hub")
    if o["op_cert_id"] != held["op_cert"]["cert_id"]:
        raise Refused("stale_op_cert")
    _dev(o, dev_mode)
    return o


def verify_uk_cert(
    signed: Any, held: Mapping[str, Any], *, now_ms: int, dev_mode: bool, revoked_op_certs: frozenset[str] = frozenset()
) -> dict[str, Any]:
    o = _under_card(signed, "hub.uk_cert", held, dev_mode, revoked_op_certs)
    _cert_time(o, now_ms)
    return o


def verify_channel_cert(
    signed: Any, held: Mapping[str, Any], *, now_ms: int, dev_mode: bool, revoked_op_certs: frozenset[str] = frozenset()
) -> dict[str, Any]:
    o = _under_card(signed, "hub.channel_cert", held, dev_mode, revoked_op_certs)
    if o["channel_id"] != channel_id(unb64u(o["key_pub"], 65)):
        raise Refused("cert_invalid")
    _cert_time(o, now_ms)
    return o


# --- HK_op: the group snapshot --------------------------------------------------------------------------------------


def verify_snapshot(
    signed: Any,
    held: Mapping[str, Any],
    *,
    workspace_id: str,
    dev_mode: bool,
    pinned: Mapping[str, Any] | None,
    revoked_op_certs: frozenset[str] = frozenset(),
) -> dict[str, Any]:
    """``pinned`` is the snapshot object the host holds (or None). Returns the object to hold next; an identical
    re-send of the pinned version is a no-op (returns the pinned object)."""
    o = _under_card(signed, "hub.snapshot", held, dev_mode, revoked_op_certs)
    roles, revoked = o["roles"], o["revoked_channels"]
    if any(not _hex16(u) or r not in ROLES or type(r) is not str for u, r in roles.items()):
        raise Refused("malformed")
    if any(not _hex16(c) for c in revoked) or revoked != sorted(set(revoked)):
        raise Refused("malformed")
    if o["workspace_id"] != workspace_id:
        raise Refused("other_workspace")
    if pinned is not None:
        if o["version"] < pinned["version"]:
            raise Refused("stale_snapshot")
        if o["version"] == pinned["version"]:
            if cj(o) != cj(pinned):
                raise Refused("snapshot_conflict")
            return dict(pinned)
    return o


def _verify_bundle(
    kind: str,
    signed: Any,
    held: Mapping[str, Any],
    workspace_id: str,
    dev_mode: bool,
    pinned: Mapping[str, Any] | None,
    revoked_op_certs: frozenset[str],
) -> dict[str, Any]:
    o = _under_card(signed, kind, held, dev_mode, revoked_op_certs)
    if o["workspace_id"] != workspace_id:
        raise Refused("other_workspace")
    if pinned is not None:
        if o["version"] < pinned["version"]:
            raise Refused("stale_bundle")
        if o["version"] == pinned["version"]:
            if cj(o) != cj(pinned):
                raise Refused("bundle_conflict")
            return dict(pinned)
    return o


def verify_org_skills(
    signed: Any,
    held: Mapping[str, Any],
    *,
    workspace_id: str,
    dev_mode: bool,
    pinned: Mapping[str, Any] | None,
    revoked_op_certs: frozenset[str] = frozenset(),
) -> dict[str, Any]:
    """M3 condition 1: a bundle names this hub and this workspace, is signed by HK_op under the verified card, and its
    ``version`` never goes down (``pinned`` is the bundle the host holds). Same version, different bytes is a conflict."""
    return _verify_bundle("hub.org_skills", signed, held, workspace_id, dev_mode, pinned, revoked_op_certs)


def verify_org_mcp(
    signed: Any,
    held: Mapping[str, Any],
    *,
    workspace_id: str,
    dev_mode: bool,
    pinned: Mapping[str, Any] | None,
    revoked_op_certs: frozenset[str] = frozenset(),
) -> dict[str, Any]:
    """The same rules for ``hub.mcp_servers`` (M3 #1435): remote servers only, offered, never enabled."""
    return _verify_bundle("hub.mcp_servers", signed, held, workspace_id, dev_mode, pinned, revoked_op_certs)


# --- UK: the custodial event ----------------------------------------------------------------------------------------

CUSTODIAL_FIELDS = (
    "auth",
    "channel",
    "channel_factor",
    "hub_id",
    "snapshot_version",
    "channel_cert",
    "uk_cert",
    "channel_proof",
)
NOT_SIGNED = ("seq", "at", "prev", "ws_seq", "sig", "host_sig")  # ticket-format §5.3
_USER_ACTOR = re.compile(r"u_[0-9a-f]{32}")
_ITEM_HASH = re.compile(r"sha256:[0-9a-f]{64}")
FACTOR_RANK = {"password+totp": 0, "se-unlocked": 1, "passkey": 2}  # D66 mapping, spec §5


#: The closed list of custodial event types (p4, protocol.md §8.5, spec D70): (type, log kind) -> the lowest hub role
#: that may start it. ``read`` starts nothing. Everything else (break-glass verbs, members, devices, roles, grants,
#: addon grants, restores, artifacts, agent-only types, host types, addon types) is ``not_custodial``: never built by
#: the hub, never accepted with ``auth: "hub"``. Policy and settings changes are not channel-startable in V3; people
#: and visibility changes are admin-only with exact schemas (phase4.ACTIONS). On top of this table the host applies ticket-format §5.4's "who may
#: sign what" with the hub role mapped to the member role (admin -> owner, edit -> member, read -> viewer).
CUSTODIAL_TYPES: dict[tuple[str, str], str] = {
    ("ticket.created", "ticket"): "edit",
    ("ticket.updated", "ticket"): "edit",
    ("status.changed", "ticket"): "edit",
    ("ticket.submitted", "ticket"): "edit",
    ("ticket.closed", "ticket"): "edit",
    ("ticket.reopened", "ticket"): "edit",
    ("claim.released", "ticket"): "edit",
    ("log.added", "ticket"): "edit",
    ("handoff.written", "ticket"): "edit",
    ("question.asked", "ticket"): "edit",
    ("question.answered", "ticket"): "edit",
    ("gate.approved", "ticket"): "edit",
    ("gate.changes_requested", "ticket"): "edit",
    ("verdict.given", "ticket"): "edit",
    ("ticket.moved", "ticket"): "admin",
    ("visibility.changed", "ticket"): "admin",
    ("people.changed", "ticket"): "admin",
    ("addon.disabled", "workspace"): "admin",
    ("addon.purged", "workspace"): "admin",
    # FB15 (§8.8b): the owner's ask to turn an add-on on; the host answers with a host-actor ``addon.granted``
    ("addon.requested", "workspace"): "admin",
    # phase 5 (§8.8, C10 M6; owner V3-D29). The host makes the grant secret itself: it admits
    # ``grant.requested`` and appends a host-actor ``grant.issued`` naming it (``grant.issued`` stays not custodial).
    ("grant.requested", "workspace"): "edit",
    ("grant.revoked", "workspace"): "edit",
    ("device.excluded", "workspace"): "admin",
    ("policy.changed", "workspace"): "admin",
    ("policy.changed", "ticket"): "edit",
    ("settings.changed", "workspace"): "admin",
}
ROLE_RANK = {"read": 1, "edit": 2, "admin": 3}
#: Selective mode (L2P4-1): the F1 member role of the canonical person -> the role floor of step 7b; full mode maps the
#: other way for step 9 (admin -> owner, edit -> member, read -> viewer).
MEMBER_ROLE_FLOOR = {"owner": "admin", "maintainer": "edit", "member": "edit", "viewer": "read"}


def log_kind(log: str) -> str:
    return "workspace" if log == "workspace" else "ticket"


def check_custodial_role(event_type: Any, log: str, role: Any) -> None:
    """Hub (before it builds) and host (§8.3 step 7): the type is custodial for this log, and the role reaches it."""
    need = CUSTODIAL_TYPES.get((event_type, log_kind(log))) if type(event_type) is str else None
    if need is None:
        raise Refused("not_custodial")
    if role not in ROLE_RANK or ROLE_RANK[role] < ROLE_RANK[need]:
        raise Refused("forbidden_role")


def signed_context(workspace_id: str, log: str, e: Mapping[str, Any]) -> dict[str, Any]:
    """ticket-format §5.3's signed-context object; E is the event without the NOT_SIGNED envelope fields."""
    return {"contract": 1, "suite": S.SUITE_ID, "workspace_id": workspace_id, "log": log, "event": dict(e)}


def event_digest(workspace_id: str, log: str, e_signed: Mapping[str, Any]) -> bytes:
    """What the channel key signs: H("orch/v2/hub-event-digest|" || cj(context of E without channel_proof))."""
    e = {k: v for k, v in e_signed.items() if k != "channel_proof"}
    try:
        return S.sha256(L["h_hub_event_digest"] + cj(signed_context(workspace_id, log, e)))
    except CanonError:
        raise Refused("malformed") from None


def custodial_bytes(workspace_id: str, log: str, e_signed: Mapping[str, Any]) -> bytes:
    try:
        return custodial_label(e_signed.get("type")) + cj(signed_context(workspace_id, log, e_signed))
    except ValueError, CanonError:
        raise Refused("malformed") from None


def channel_proof_bytes(digest: bytes) -> bytes:
    return L["sig_hub_channel_proof"] + digest


def verify_custodial(
    event: Any,
    *,
    workspace_id: str,
    log: str,
    held: Mapping[str, Any],
    snapshot: Mapping[str, Any],
    now_ms: int,
    dev_mode: bool,
    mode: str = "full",
    granted_users: frozenset[str] = frozenset(),
    min_factor: str | None = None,
    revoked_op_certs: frozenset[str] = frozenset(),
    member_roles: Mapping[str, str] = {},
    live_items: Mapping[str, Any] = {},
    superseded_items: frozenset[str] = frozenset(),
    log_heads: Mapping[str, list[str]] = {},
) -> dict[str, Any]:
    """The host's check of a custodial event (spec §5 "Verification in the workspace"). ``event`` is the event as
    delivered (E plus ``sig``; the host adds ``seq``/``at``/``prev``/``ws_seq``/``host_sig`` only when it appends).
    The F1 checks of the event type itself (gate hash, roles of the ticket, ...) run after this one, as for any person
    event. Returns the user id and the channel."""
    if type(event) is not dict or any(k in event for k in ("seq", "at", "prev", "ws_seq", "host_sig")):
        raise Refused("malformed")
    # 1. shape of the hub fields
    if event.get("auth") != "hub" or any(k not in event for k in (*CUSTODIAL_FIELDS, "sig", "type", "actor")):
        raise Refused("malformed")
    actor, typ = event["actor"], event["type"]
    if type(typ) is not str or not EVENT_TYPE.fullmatch(typ):
        raise Refused("malformed")
    if (
        type(actor) is not dict
        or set(actor) != {"kind", "id", "via"}
        or actor["kind"] != "person"
        or type(actor["id"]) is not str
        or not _USER_ACTOR.fullmatch(actor["id"])
        or actor["via"] != event["channel"]
    ):
        raise Refused("malformed")
    if (
        not _hex16(event["channel"])
        or event["channel_factor"] not in FACTORS
        or not _hex16(event["hub_id"])
        or not _int_min(1)(event["snapshot_version"])
    ):
        raise Refused("malformed")
    # C3b addendum (§8.1): ``item_hash`` (the shared item the person saw) exactly in selective mode
    if ("item_hash" in event) != (mode == "selective") or (
        "item_hash" in event and (type(event["item_hash"]) is not str or not _ITEM_HASH.fullmatch(event["item_hash"]))
    ):
        raise Refused("malformed")
    # selective mode (§8.6): only the four decisions, on a ticket log, text-only answers
    from .phase4 import SELECTIVE_TYPES, check_custodial_payload

    if mode == "selective":
        if typ not in SELECTIVE_TYPES or log == "workspace":
            raise Refused("not_custodial")
        if typ == "question.answered" and "option" in event:
            raise Refused("malformed")
    # the exact payload of the type (§8.5 event side, L2-C3-1); a type outside the table is refused at 7b
    if (typ, log_kind(log)) in CUSTODIAL_TYPES:
        check_custodial_payload(event, log)
    user = actor["id"][2:]
    # 2. the hub this workspace is bound to
    if event["hub_id"] != held["card"]["hub_id"]:
        raise Refused("other_hub")
    # 3. UK -> HK_op cert -> pinned HK_root (held was verified against the pin and the revocations)
    uk = verify_uk_cert(event["uk_cert"], held, now_ms=now_ms, dev_mode=dev_mode, revoked_op_certs=revoked_op_certs)
    if uk["user_id"] != user:
        raise Refused("other_user")
    # 4. the channel certificate
    ch = verify_channel_cert(
        event["channel_cert"], held, now_ms=now_ms, dev_mode=dev_mode, revoked_op_certs=revoked_op_certs
    )
    if ch["user_id"] != user or ch["channel_id"] != event["channel"]:
        raise Refused("other_user")
    if ch["factor"] != event["channel_factor"]:
        raise Refused("factor_mismatch")
    # 5. the UK signature, under hub-custodial|<type>|
    e_signed = {k: v for k, v in event.items() if k not in NOT_SIGNED}
    try:
        sig = unb64u(event["sig"], S.SIG_LEN)
        proof = unb64u(event["channel_proof"], S.SIG_LEN)
    except CanonError:
        raise Refused("malformed") from None
    if not S.verify(unb64u(uk["uk_pub"], 65), sig, custodial_bytes(workspace_id, log, e_signed)):
        raise Refused("bad_signature")
    # 6. the channel proof over the event digest (enforced in core from phase 4, C3/D6)
    if not S.verify(unb64u(ch["key_pub"], 65), proof, channel_proof_bytes(event_digest(workspace_id, log, e_signed))):
        raise Refused("bad_channel_proof")
    # 7. snapshot: role and channel revocation against the newest snapshot the host holds
    if event["snapshot_version"] > snapshot["version"]:
        raise Refused("unknown_snapshot")
    if event["channel"] in snapshot["revoked_channels"]:
        raise Refused("channel_revoked")
    if mode == "selective":
        if user not in granted_users:
            raise Refused("not_granted")
        item = live_items.get(event["item_hash"])
        if item is None and event["item_hash"] in superseded_items:
            raise Refused("event_changed")  # an earlier version of a live item: the person may sign again
        if type(item) is not dict or type(item.get("chain")) is not dict or type(item.get("f1")) is not dict:
            raise Refused("not_granted")
        _selective_item(event, log, item["chain"], item["f1"], log_heads)
    elif snapshot["roles"].get(user) not in ROLES:
        raise Refused("forbidden_role")
    # 7b. the closed list of custodial types and the lowest role per type (p4, §8.5; selective: owner-granted users,
    # and still only custodial types)
    # L2P4-1: in selective mode there is no hub role; the floor comes from the F1 member role of the canonical person
    # (the user's alias to a member p_, §9.1), and a granted user without an alias is a viewer (signs nothing).
    if mode == "selective":
        floor = MEMBER_ROLE_FLOOR.get(member_roles.get(user, "viewer"))  # unknown role -> None -> forbidden_role
    else:
        floor = snapshot["roles"].get(user)
    check_custodial_role(typ, log, floor)
    # 8. D66 factor policy
    if min_factor is not None and FACTOR_RANK[event["channel_factor"]] < FACTOR_RANK[min_factor]:
        raise Refused("factor_too_weak")
    return {"user_id": user, "channel": event["channel"], "role": snapshot["roles"].get(user)}


def _selective_item(
    event: Mapping[str, Any],
    log: str,
    chain: Mapping[str, Any],
    f1: Mapping[str, Any],
    log_heads: Mapping[str, list[str]],
) -> None:
    """The item check at admission (§8.3 step 7, §8.6, core §15.7). ``live_items`` = the host's live shared items
    ``{item_hash: {"chain": {log, seq, head}, "f1": {...}}}`` (current versions only); ``superseded`` = hashes of
    earlier versions of live items; ``log_heads`` = ``{log: [head of seq 1, 2, ...]}`` of the host's own logs.
    1. the hash is a current live item (an earlier version: ``event_changed``, the person may sign again; else
    ``not_granted``); 2. its ``chain.log`` is the event's log and the decision's target is in its ``f1`` (``gates[gate]``,
    ``verify`` for a verdict, ``questions[question]``) → else ``not_granted``; 3. ``chain.head`` is the head of
    ``chain.log`` at ``chain.seq`` in the host's log → else ``view_fork``. Anything unreadable refuses."""
    if chain.get("log") != log:
        raise Refused("not_granted")
    typ = event["type"]
    if typ == "question.answered":
        targets, key = f1.get("questions"), event.get("question")
    else:
        targets, key = f1.get("gates"), "verify" if typ == "verdict.given" else event.get("gate")
    if type(targets) is not dict or type(key) is not str or key not in targets:
        raise Refused("not_granted")
    # the event is built on exactly this item's values (what the person was shown); another value → event_changed
    t = targets[key]
    if type(t) is not dict:
        raise Refused("not_granted")
    want = {"hash": t.get("hash"), "based_on": chain.get("head"), "roster_v": f1.get("roster_v")}
    if typ != "question.answered":
        want.update(gate_gen=t.get("gen"), policy_hash=t.get("policy_hash"))
    if "source_sha" in event:  # step 1 already requires it exactly on a code approval and a verdict
        want["source_sha"] = t.get("source_sha")
    if any(v is None or event.get(k) != v for k, v in want.items()):
        raise Refused("event_changed")
    heads, seq = log_heads.get(log), chain.get("seq")
    if (
        type(heads) is not list
        or type(seq) is not int
        or not 1 <= seq <= len(heads)
        or heads[seq - 1] != chain.get("head")
    ):
        raise Refused("view_fork")


# --- WSK: binding and unbound; the publish-side store ---------------------------------------------------------------


def verify_binding(signed: Any, *, workspace_id: str, wsk_pub: bytes, now_ms: int, dev_mode: bool) -> dict[str, Any]:
    """``workspace_id`` is the workspace the binding is presented for (the route, the request); ``wsk_pub`` the key of
    that workspace's card as the verifier already holds it (protocol-v2 §12.1 step 3)."""
    o = _envelope(signed, "hub.binding")
    _signature(signed, L["sig_hub_binding"], wsk_pub)
    _fields(o, "hub.binding")
    if o["workspace_id"] != workspace_id:
        raise Refused("other_workspace")
    if unb64u(o["wsk_pub"], 65) != wsk_pub:
        raise Refused("wsk_mismatch")
    if o["hub_id"] != hub_id(unb64u(o["hk_root_pub"], 65)):
        raise Refused("hub_id_mismatch")
    _dev(o, dev_mode)
    if o["valid_from_ms"] > now_ms + CLOCK_SKEW_MS:
        raise Refused("not_yet_valid")
    return o


def verify_unbound(signed: Any, *, workspace_id: str, wsk_pub: bytes, dev_mode: bool) -> dict[str, Any]:
    o = _envelope(signed, "hub.unbound")
    _signature(signed, L["sig_hub_unbound"], wsk_pub)
    _fields(o, "hub.unbound")
    if o["workspace_id"] != workspace_id:
        raise Refused("other_workspace")
    _dev(o, dev_mode)
    return o


def apply_binding(state: Mapping[str, Any] | None, b: Mapping[str, Any]) -> dict[str, Any]:
    """Binding store transition for one workspace. ``state`` is ``{"binding_seq", "status": "bound"|"unbound",
    "binding": o}`` or None. A binding at or below an unbound sequence never comes back."""
    if state is not None:
        if b["binding_seq"] <= state["binding_seq"] and state["status"] == "unbound":
            raise Refused("unbound")
        if b["binding_seq"] < state["binding_seq"]:
            raise Refused("stale_binding")
        if b["binding_seq"] == state["binding_seq"]:
            if cj(dict(b)) != cj(dict(state["binding"])):
                raise Refused("binding_conflict")
            return dict(state)
    return {"binding_seq": b["binding_seq"], "status": "bound", "binding": dict(b)}


def apply_unbound(state: Mapping[str, Any] | None, u: Mapping[str, Any]) -> dict[str, Any]:
    """An unbound at ``binding_seq`` ends that binding and every lower one, also one not yet seen."""
    if state is not None and u["binding_seq"] < state["binding_seq"]:
        raise Refused("stale_binding")
    if (
        state is not None
        and state["status"] == "bound"
        and u["binding_seq"] == state["binding_seq"]
        and u["hub_id"] != state["binding"]["hub_id"]
    ):
        raise Refused("other_hub")
    return {
        "binding_seq": u["binding_seq"],
        "status": "unbound",
        "binding": None if state is None else state["binding"],
    }


# --- the hub's trust in a workspace at join (L2-3) ------------------------------------------------------------------

V2_DELEGATION_FIELDS = {
    "v",
    "suite",
    "kind",
    "workspace_id",
    "wsk_pub",
    "owner_person_id",
    "client_hosted",
    "issued_ms",
}
V2_CARD_FIELDS = {
    "v",
    "suite",
    "kind",
    "workspace_id",
    "wsk_pub",
    "wxk_pub",
    "wxk_version",
    "owner_person_id",
    "relay_url",
    "card_seq",
    "issued_ms",
    "sealed_hash",
    "ck_commit",
}


def verify_v2_card(card: Any, pk_pub: bytes, *, now_ms: int) -> dict[str, Any]:
    """A workspace's v2 directory card (protocol-v2 §7.1): the owner's PK-signed delegation names ``WSK`` once, the
    card is ``WSK``-signed. The sealed part is not opened; ``sealed_hash`` binds it. Returns the card's ``o``."""
    if (
        type(card) is not dict
        or set(card) != {"delegation", "o", "sig", "sealed"}
        or type(card["delegation"]) is not dict
        or set(card["delegation"]) != {"o", "sig"}
    ):
        raise Refused("malformed")
    d, o = card["delegation"]["o"], card["o"]
    if type(d) is not dict or type(o) is not dict or set(d) != V2_DELEGATION_FIELDS or set(o) != V2_CARD_FIELDS:
        raise Refused("malformed")
    if d["v"] != 2 or o["v"] != 2 or d["kind"] != "ws_delegation" or o["kind"] != "card":
        raise Refused("malformed")
    if d["suite"] != S.SUITE_ID or o["suite"] != S.SUITE_ID:
        raise Refused("wrong_suite")
    if not (
        _hex16(d["workspace_id"])
        and _pub(d["wsk_pub"])
        and _hex16(d["owner_person_id"])
        and _pub(o["wxk_pub"])
        and _int_min(1)(o["card_seq"])
        and _b64u_n(32)(o["sealed_hash"])
        and _b64u_n(32)(o["ck_commit"])
    ):
        raise Refused("malformed")
    if person_id(pk_pub) != d["owner_person_id"]:
        raise Refused("other_person")
    _signature(card["delegation"], L["sig_ws_delegation"], pk_pub)
    if (o["workspace_id"], o["wsk_pub"], o["owner_person_id"]) != (
        d["workspace_id"],
        d["wsk_pub"],
        d["owner_person_id"],
    ):
        raise Refused("delegation_mismatch")
    _signature(card, L["sig_card_wsk"], unb64u(d["wsk_pub"], 65))
    try:
        if b64u(S.sha256(unb64u(card["sealed"]))) != o["sealed_hash"]:
            raise Refused("malformed")
    except CanonError:
        raise Refused("malformed") from None
    if type(o["issued_ms"]) is not int or o["issued_ms"] > now_ms + CLOCK_SKEW_MS:
        raise Refused("malformed")
    return o


def check_hub_join(
    join: Any,
    *,
    hub_id_: str,
    pins: Mapping[str, Mapping[str, str]],
    now_ms: int,
    dev_mode: bool,
    invite_workspace_id: str | None = None,
) -> dict[str, str]:
    """The hub's check of a workspace joining (L2-3). ``join`` = {card (v2 directory card), pk_pub (the owner's PK),
    binding}. ``pins`` = {workspace_id: {owner_person_id, wsk_pub}}, first come (TOFU per workspace_id). Returns the
    pin for this workspace. The binding is self-signed by the key it names, so without the card it proves nothing."""
    if type(join) is not dict or set(join) - {"card", "pk_pub", "binding"} or "binding" not in join:
        raise Refused("malformed")
    if "card" not in join or "pk_pub" not in join:
        raise Refused("unknown_workspace")
    if not _pub(join["pk_pub"]):
        raise Refused("malformed")
    co = verify_v2_card(join["card"], unb64u(join["pk_pub"], 65), now_ms=now_ms)
    ws = co["workspace_id"]
    if invite_workspace_id is not None and ws != invite_workspace_id:
        raise Refused("other_workspace")
    b = verify_binding(
        join["binding"], workspace_id=ws, wsk_pub=unb64u(co["wsk_pub"], 65), now_ms=now_ms, dev_mode=dev_mode
    )
    if b["hub_id"] != hub_id_:
        raise Refused("other_hub")
    pin = pins.get(ws)
    if pin is not None:
        if pin["owner_person_id"] != co["owner_person_id"]:
            raise Refused("other_person")
        if pin["wsk_pub"] != co["wsk_pub"]:
            raise Refused("wsk_mismatch")
    return {"workspace_id": ws, "owner_person_id": co["owner_person_id"], "wsk_pub": co["wsk_pub"]}


# --- the hub.joined payload and aliases ------------------------------------------------------------------------------


def _joined_shape(p: Any) -> bool:
    """The members of a ``hub.joined`` payload other than ``owner_proof`` (types and ``hub_id`` only)."""
    return (
        type(p) is dict
        and set(p) == JOINED_KEYS
        and _pub(p["hk_root_pub"])
        and _hex16(p["hub_id"])
        and p["mode"] in MODES
        and _int_min(1)(p["binding_seq"])
        and type(p["aliases"]) is list
    )


def _left_shape(p: Any) -> bool:
    return type(p) is dict and set(p) == LEFT_KEYS and _hex16(p["hub_id"]) and _int_min(1)(p["binding_seq"])


def _proof_member(p: Any, keys: frozenset[str]) -> dict[str, Any]:
    """Split ``owner_proof`` off an event payload: the member is required and is a signed object (§9.1); ``null`` is
    ``proof_missing`` (every hub op carries the owner proof, the terminal path too)."""
    if type(p) is not dict or set(p) != {*keys, "owner_proof"}:
        raise Refused("malformed")
    proof = p["owner_proof"]
    if proof is None:
        raise Refused("proof_missing")
    if type(proof) is not dict:
        raise Refused("malformed")
    return {k: v for k, v in p.items() if k != "owner_proof"}


def check_joined_payload(p: Any, *, prior_aliases: Mapping[str, str] = {}) -> dict[str, str]:
    """The payload of the workspace-log event ``hub.joined`` (signed by the owner's device key like any person event,
    ``orch/v2/sig/ws-event|``). Returns the alias map person_id -> user_id after it. Aliases are never re-pointed.
    The ``owner_proof`` member is checked here for its place only; :func:`check_recorded_owner_proof` verifies it."""
    return _joined_aliases(_proof_member(p, JOINED_KEYS), prior_aliases)


def _joined_aliases(q: Any, prior_aliases: Mapping[str, str]) -> dict[str, str]:
    """The ``hub.joined`` payload without ``owner_proof``: shape, ``hub_id``, aliases (unique both ways, sorted)."""
    if not _joined_shape(q):
        raise Refused("malformed")
    if q["hub_id"] != hub_id(unb64u(q["hk_root_pub"], 65)):
        raise Refused("hub_id_mismatch")
    out = dict(prior_aliases)
    users = {u: pid for pid, u in prior_aliases.items()}
    pids = []
    for a in q["aliases"]:
        if (
            type(a) is not dict
            or set(a) != {"person_id", "user_id"}
            or not (_hex16(a["person_id"]) and _hex16(a["user_id"]))
        ):
            raise Refused("malformed")
        pid, uid = a["person_id"], a["user_id"]
        pids.append(pid)
        if out.get(pid, uid) != uid or users.get(uid, pid) != pid:
            raise Refused("alias_repointed")
        out[pid], users[uid] = uid, pid
    if pids != sorted(set(pids)):
        raise Refused("malformed")
    return out


def check_left_payload(p: Any) -> dict[str, Any]:
    """The payload of ``hub.left``: ``{hub_id, binding_seq, owner_proof}`` exactly (§9.1)."""
    q = _proof_member(p, LEFT_KEYS)
    if not _left_shape(q):
        raise Refused("malformed")
    return q


# --- the owner proof of a hub op (#1198 C8, §9.6) -------------------------------------------------------------------


def _proof_payload_ok(op: str, p: Any) -> bool:
    """The payload a proof names has the exact shape of its event's payload without ``owner_proof``: ``hub.join`` and
    ``hub.mode`` a ``hub.joined`` payload (``hub.mode`` only to ``full``, §9.1 "Mode switch"), ``hub.leave`` a
    ``hub.left`` payload."""
    if op == "hub.leave":
        return _left_shape(p)
    if not _joined_shape(p) or (op == "hub.mode" and p["mode"] != "full"):
        return False
    try:
        if p["hub_id"] != hub_id(unb64u(p["hk_root_pub"], 65)):
            return False
        _joined_aliases(p, {})
    except Refused, CanonError:
        return False
    return True


def _owner_proof(signed: Any, pk_pub: Any) -> dict[str, Any]:
    """§9.6 steps 1-4: one code, ``proof_invalid``, for every way the object itself is wrong (the person is never
    asked to tell a bad signature from a bad field; nothing is echoed)."""
    if type(pk_pub) is not bytes or not S.valid_public_key(pk_pub):
        raise Refused("proof_invalid")
    try:
        o = _envelope(signed, "hub.owner_proof")
        _signature(signed, L["sig_hub_owner_proof"], pk_pub)
        _fields(o, "hub.owner_proof")
    except Refused:
        raise Refused("proof_invalid") from None
    if o["person_id"] != person_id(pk_pub) or not _proof_payload_ok(o["op"], o["payload"]):
        raise Refused("proof_invalid")
    if o["expires_ms"] <= o["issued_ms"] or o["expires_ms"] - o["issued_ms"] > OWNER_PROOF_MAX_MS:
        raise Refused("proof_invalid")
    return o


def _proof_time(o: Mapping[str, Any], now_ms: int) -> None:
    if o["issued_ms"] > now_ms + CLOCK_SKEW_MS:
        raise Refused("proof_invalid")
    if now_ms >= o["expires_ms"]:
        raise Refused("proof_expired")


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
    """§9.6 "At the host": the proof a hub op carries, at binding and again at submit. ``pk_pub`` is the acting
    owner's person key from the roster; ``payload`` the event payload the op signs, without ``owner_proof``;
    ``request_id`` the host request being bound or submitted. Every argument is required: nothing is skipped."""
    o = _owner_proof(signed, pk_pub)
    if o["workspace_id"] != workspace_id or o["op"] != op or o["request_id"] != request_id:
        raise Refused("proof_invalid")
    try:
        same = cj(o["payload"]) == cj(dict(payload))
    except CanonError:
        same = False
    if not same:
        raise Refused("proof_invalid")
    _proof_time(o, now_ms)
    _dev(o, dev_mode)
    return o


def check_join_proof(
    signed: Any,
    *,
    pk_pub: bytes,
    workspace_id: str,
    hub_id_: str,
    binding: Mapping[str, Any],
    op: str,
    now_ms: int,
    dev_mode: bool,
) -> dict[str, Any]:
    """§9.6 "At the hub": the proof with a link ``join`` (``op`` ``hub.join``) or a mode-switch ``binding_put``
    (``op`` ``hub.mode``). ``pk_pub`` is the owner key the join's card was verified under (§9.5), ``binding`` the
    verified ``hub.binding`` object. The hub has no request id and no alias book to compare: it checks the workspace,
    the op, and that the proof names this hub, this root, this mode and this ``binding_seq``."""
    if op not in ("hub.join", "hub.mode"):
        raise Refused("proof_invalid")
    if signed is None:
        raise Refused("proof_missing")  # the hub always requires the proof, whatever the host sends (§9.6)
    o = _owner_proof(signed, pk_pub)
    p = o["payload"]
    if (
        o["workspace_id"] != workspace_id
        or o["op"] != op
        or p["hub_id"] != hub_id_
        or p["hub_id"] != binding["hub_id"]
        or p["hk_root_pub"] != binding["hk_root_pub"]
        or p["mode"] != binding["mode"]
        or p["binding_seq"] != binding["binding_seq"]
    ):
        raise Refused("proof_invalid")
    _proof_time(o, now_ms)
    _dev(o, dev_mode)
    return o


RECORDED_OPS = {"hub.joined": ("hub.join", "hub.mode"), "hub.left": ("hub.leave",)}


PRIOR_TYPES = ("hub.joined", "hub.left")


def _placement(event_type: str, op: str, payload: Mapping[str, Any], prior: Any) -> None:
    """§9.6 "Readers" step 2: where the event may stand, from the reader's own record of the last ``hub.joined`` /
    ``hub.left`` before it (``prior``: ``None``, or ``{type, hub_id, mode, binding_seq}``; ``mode`` is ``None`` after
    a leave). A join follows nothing or a leave; a mode switch follows a selective join of the same hub; a leave
    follows a join of the same hub and names its ``binding_seq``; a join or mode switch takes the next one."""
    if prior is not None and not (
        type(prior) is dict
        and set(prior) == {"type", "hub_id", "mode", "binding_seq"}
        and prior["type"] in PRIOR_TYPES
        and _hex16(prior["hub_id"])
        and _int_min(1)(prior["binding_seq"])
        and (prior["mode"] in MODES if prior["type"] == "hub.joined" else prior["mode"] is None)
    ):
        raise Refused("malformed")
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
        raise Refused("stale_binding")


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
    """§9.6 "Readers": a ``hub.joined`` / ``hub.left`` on the workspace log (the host model, another member's core,
    the hub's log view). Every such event carries a valid owner proof (``null`` is ``proof_missing``), whatever its
    ``auth``. The verifier's own facts, never fields the signer or a deliverer chose: ``at_ms`` (the append time from
    the host's chained, host-signed record), ``prior`` (its own record of the last ``hub.joined`` / ``hub.left``,
    see :func:`_placement`), ``pk_pub`` (the actor's ``PK`` from the roster, ``None`` when unknown: refused).
    Order: the proof object and its bindings, the event's place (``stale_binding``), the append time inside the
    proof's life (``issued_ms - 300 s <= at_ms <= expires_ms``, ``proof_expired``), the dev marker."""
    allowed = RECORDED_OPS.get(event_type)
    if allowed is None or not _int_min(0)(at_ms):
        raise Refused("malformed")
    rest = _proof_member(payload, JOINED_KEYS if event_type == "hub.joined" else LEFT_KEYS)
    if pk_pub is None:
        raise Refused("proof_invalid")
    o = _owner_proof(payload["owner_proof"], pk_pub)
    if o["workspace_id"] != workspace_id or o["person_id"] != actor_person_id or o["op"] not in allowed:
        raise Refused("proof_invalid")
    try:
        same = cj(o["payload"]) == cj(rest)
    except CanonError:
        same = False
    if not same:
        raise Refused("proof_invalid")
    _placement(event_type, o["op"], rest, prior)
    if not o["issued_ms"] - CLOCK_SKEW_MS <= at_ms <= o["expires_ms"]:
        raise Refused("proof_expired")
    _dev(o, dev_mode)
    return o


# --- link messages ------------------------------------------------------------------------------------------------

#: op -> (direction of the request, mode it exists in or None for both, exact body fields)
#: body members a request may leave out (everything else in LINK_OPS is required)
PROFILE_FIELDS: dict[str, Check] = {
    "listed": _bool,
    "tags": lambda x: type(x) is list and len(x) <= 8 and all(type(v) is str and len(v) <= 24 for v in x),
    "handles": lambda x: type(x) is list and len(x) <= 8 and all(type(v) is str and len(v) <= 80 for v in x),
    "not_handled": lambda x: type(x) is list and len(x) <= 8 and all(type(v) is str and len(v) <= 80 for v in x),
    "accepts": lambda x: type(x) is list and len(x) <= 8 and all(type(v) is str and len(v) <= 24 for v in x),
    "description": lambda x: type(x) is str and len(x) <= 300,
    "contact": lambda x: type(x) is str and len(x) <= 80,
    "languages": lambda x: type(x) is str and len(x) <= 40,
    "hours": lambda x: type(x) is str and len(x) <= 40,
}

LINK_OPTIONAL: dict[str, frozenset[str]] = {"join": frozenset({"alias_users"}), "directory_get": frozenset({"query"})}
LINK_OPS: dict[str, tuple[str, str | None, dict[str, Check]]] = {
    "snapshot_get": ("w2h", None, {}),
    "mcp_servers_get": ("w2h", None, {}),  # M3 (#1435): the signed hub.mcp_servers of the link's workspace
    "org_skills_get": ("w2h", None, {}),  # M3 (#1430): the signed hub.org_skills of the link's workspace
    "event_deliver": ("h2w", None, {"log": lambda x: type(x) is str, "event": lambda x: type(x) is dict}),
    "route_out": ("w2h", None, {"envelope": _b64u_any}),
    "route_in": ("h2w", None, {"envelope": _b64u_any}),
    "wk_seal": ("w2h", "full", {"epoch": _int_min(1), "wrap": _b64u_any}),
    "publish_out": ("w2h", None, {"request": lambda x: type(x) is dict}),
    "join": (
        "w2h",
        None,
        {
            "invite": lambda x: type(x) is str,
            "card": lambda x: type(x) is dict,
            "pk_pub": _pub,
            "binding": lambda x: type(x) is dict,
            "owner_proof": lambda x: x is None or type(x) is dict,  # #1198 C9: required member; null is proof_missing
            # optional (run 12): the hub users the owner-signed aliases name; the hub holds them to the invite's owner
            "alias_users": lambda x: type(x) is list and all(type(u) is str for u in x),
        },
    ),
    "card_put": ("w2h", None, {"card": lambda x: type(x) is dict, "pk_pub": _pub}),
    "binding_put": (
        "w2h",
        None,
        {
            "object": lambda x: type(x) is dict,
            # #1198 C9: a binding that is new or switches the mode needs it (null is proof_missing); an unbound or a
            # re-sent identical binding takes null
            "owner_proof": lambda x: x is None or type(x) is dict,
        },
    ),
    "audit_ack": ("w2h", None, {"seq": _int_min(1), "head": _b64u_n(32)}),
    "share_put": ("w2h", "selective", {"item_id": _hex16, "sealed": _b64u_any}),
    "share_revoke": ("w2h", "selective", {"item_id": _hex16}),
    # D4 audit (§10.5): the hub's chain is global; a workspace pulls it and acknowledges the head it verified
    "audit_get": ("w2h", None, {"since": _int_min(0)}),
    # V3-D8 shared spaces (§10.4): both modes; what a selective workspace may put is its owner's share rules (host side)
    "space_list": ("w2h", None, {}),
    "space_put": ("w2h", None, {"space_id": _hex16, "epoch": _int_min(1), "item_id": _hex16, "sealed": _b64u_any}),
    "space_accept": ("w2h", None, {"accept": lambda x: type(x) is dict}),
    "space_revoke": ("w2h", None, {"space_id": _hex16, "item_id": _hex16}),
    "space_get": ("w2h", None, {"space_id": _hex16, "since": _int_min(0)}),
    # p4 (§10.4, #1020): the pending hub.space_proposal objects that name the link's workspace
    "space_proposals": ("w2h", None, {}),
    # hub one-app (TECH.md 27 Oct): the browser switch between a workspace dashboard and the hub console
    "switch_ticket": (
        "w2h",
        None,
        {
            "origin": lambda x: type(x) is str,
            "return_to": lambda x: type(x) is str,
            "kind": lambda x: x in ("open", "signout"),
        },
    ),
    "switch_redeem": ("w2h", None, {"ticket": lambda x: type(x) is str}),
    "rail_get": ("w2h", None, {"origin": lambda x: type(x) is str}),
    "profile_put": ("w2h", None, PROFILE_FIELDS),
    "directory_get": ("w2h", None, {"query": lambda x: type(x) is str and len(x) <= 80}),
    "status_put": (
        "w2h",
        None,
        {
            "version": lambda x: type(x) is str and len(x) <= 32 and re.fullmatch(r"[0-9A-Za-z.+-]+", x) is not None,
            "commit": lambda x: type(x) is str and len(x) <= 40 and re.fullmatch(r"[0-9a-fA-F]*", x) is not None,
            "host": lambda x: type(x) is str and len(x) <= 80,
            "name": lambda x: type(x) is str and len(x) <= 80,  # the workspace's display name; kept in full mode only
        },
    ),
    # M3 (#1450): a host tells the hub an add-on install was refused by the policy; display data, never an authority
    "addon_request": (
        "w2h",
        None,
        {
            "name": lambda x: type(x) is str and re.fullmatch(r"[a-z][a-z0-9-]{0,39}", x) is not None,
            "version": lambda x: type(x) is str and re.fullmatch(r"[0-9A-Za-z.+-]{0,32}", x) is not None,
            "actor": lambda x: x in ("person", "agent"),  # who asked at the host (the host knows; shown, never trusted)
        },
    ),
    "owner_get": ("w2h", None, {}),  # FB15: the hub user the hub names as the workspace owner (no argument)
    # p4 (§10.7): the hub's WK_e-sealed view of one workspace log; full mode only (selective: the hub holds no WK_e)
    "log_sync": (
        "w2h",
        "full",
        {
            "epoch": _int_min(1),
            "log": lambda x: type(x) is str,
            "from_seq": _int_min(1),
            "count": _int_min(1),
            "head": lambda x: type(x) is str,
            "reset": _bool,
            "sealed": _b64u_any,
        },
    ),
}


def link_label(direction: str) -> bytes:
    return L["sig_hub_link"] if direction == "w2h" else L["sig_hub_route"]


def verify_link(
    signed: Any, *, direction: str, hub_id_: str, workspace_id: str, mode: str, pub: bytes, now_ms: int
) -> dict[str, Any]:
    """``pub`` is WSK for w2h (checked by the hub) and the card's HK_op for h2w (checked by the host). Requests are
    messages with ``re: null`` in the op's request direction; a reply has ``re`` = the request's ``seq`` and the other
    direction, and its body is either the op's reply body or ``{"error": code}`` (not validated in H0)."""
    o = _envelope(signed, "hub.link")
    _signature(signed, link_label(direction), pub)
    _fields(o, "hub.link")
    if o["dir"] != direction:
        raise Refused("malformed")
    spec = LINK_OPS.get(o["op"])
    if spec is None:
        raise Refused("unknown_op")
    req_dir, op_mode, body = spec
    is_request = o["re"] is None
    if (o["dir"] == req_dir) != is_request:
        raise Refused("unknown_op")
    if op_mode is not None and op_mode != mode:
        raise Refused("wrong_mode")
    if o["hub_id"] != hub_id_:
        raise Refused("other_hub")
    if o["workspace_id"] != workspace_id:
        raise Refused("other_workspace")
    if abs(now_ms - o["ts_ms"]) > LINK_WINDOW_MS:
        raise Refused("stale_timestamp")
    optional = LINK_OPTIONAL.get(o["op"], frozenset())
    if is_request and (
        not set(body) - optional <= set(o["body"]) <= set(body)
        or not all(ok(o["body"][k]) for k, ok in body.items() if k in o["body"])
    ):
        raise Refused("malformed")
    return o


def link_auth_bytes(hub_id_: str, workspace_id: str, challenge: bytes, exporter: bytes) -> bytes:
    """What WSK signs to open a link over TLS: the hub's 32-byte challenge bound to the 32-byte TLS exporter
    (label ``EXPORTER-orch-hub-link``, empty context)."""
    if len(challenge) != 32 or len(exporter) != 32:
        raise Refused("malformed")
    return (
        L["sig_hub_link_auth"]
        + bytes([S.SUITE_ID])
        + unhex(hub_id_, 16)
        + unhex(workspace_id, 16)
        + challenge
        + exporter
    )


# --- publish through the hub (phase 2, D76, V3-D4) ------------------------------------------------------------------


def _lp16(b: bytes) -> bytes:
    if len(b) >= 1 << 16:
        raise Refused("malformed")
    return struct.pack(">H", len(b)) + b


def publish_signed_bytes(
    ws: bytes, ts_ms: int, nonce: bytes, method: str, host: str, target: str, body: bytes
) -> bytes:
    """protocol-v2 §12.1, unchanged."""
    return (
        L["sig_publish"]
        + bytes([S.SUITE_ID])
        + ws
        + struct.pack(">Q", ts_ms)
        + nonce
        + _lp16(method.encode("ascii"))
        + _lp16(host.encode("ascii"))
        + _lp16(target.encode("ascii"))
        + S.sha256(body)
    )


def _request_fields(ws: bytes, ts_ms: int, nonce: bytes, method: str, host: str, target: str, body: bytes) -> bytes:
    """The v2 §12.1 fields after the label, in v2 order."""
    return (
        bytes([S.SUITE_ID])
        + ws
        + struct.pack(">Q", ts_ms)
        + nonce
        + _lp16(method.encode("ascii"))
        + _lp16(host.encode("ascii"))
        + _lp16(target.encode("ascii"))
        + S.sha256(body)
    )


def approval_hash(approval: Mapping[str, Any] | None) -> bytes:
    """``H(cj(approval))``, or 32 zero bytes when the request carries none."""
    if approval is None:
        return bytes(32)
    try:
        return S.sha256(cj(dict(approval)))
    except CanonError:
        raise Refused("malformed") from None


def hub_path_signed_bytes(
    ws: bytes,
    ts_ms: int,
    nonce: bytes,
    method: str,
    host: str,
    target: str,
    body: bytes,
    hub_id_: str,
    approval: Mapping[str, Any] | None,
) -> bytes:
    """What WSK signs on the hub path (L2-2): its own label, the v2 §12.1 fields, the hub, and the approval the host
    attaches (or none). A hub-path signature never verifies as a direct request, the host commits to the hub that
    forwards it, and nobody can add, swap or strip an approval after the host signed."""
    return (
        L["sig_publish_hub"]
        + _request_fields(ws, ts_ms, nonce, method, host, target, body)
        + unhex(hub_id_, 16)
        + approval_hash(approval)
    )


def hub_publish_bytes(hub_id_: str, wsk_bytes: bytes) -> bytes:
    """What HK_op signs when forwarding: the exact hub-path WSK-signed bytes, by hash, bound to the hub."""
    return L["sig_hub_publish"] + bytes([S.SUITE_ID]) + unhex(hub_id_, 16) + S.sha256(wsk_bytes)


def publish_request_hash(
    ws: bytes, ts_ms: int, nonce: bytes, method: str, host: str, target: str, body: bytes, hub_id_: str
) -> bytes:
    """What the owner's approval names (L2-2): the one exact request, ts and nonce included, through this hub. The
    host draws ts and nonce first and the owner signs after, so an approval is good for one request (the nonce store)
    within the 120 s window."""
    return S.sha256(
        L["h_hub_publish_request"] + _request_fields(ws, ts_ms, nonce, method, host, target, body) + unhex(hub_id_, 16)
    )


DEVICE_CERT_FIELDS = {
    "v",
    "suite",
    "kind",
    "device_id",
    "person_id",
    "dk_sig_pub",
    "dk_kx_pub",
    "label_sealed",
    "created_ms",
    "expires_ms",
    "scopes_max",
}
_LEVELS = ["look", "decide", "operate", "type"]


def verify_device_cert(signed: Any, pk_pub: bytes, *, now_ms: int, revoked_devices: frozenset[str]) -> dict[str, Any]:
    """protocol-v2 §6.1, as orch-publish's relay_proto.verify_cert; here only as the approval's chain."""
    if type(signed) is not dict or set(signed) != {"o", "sig"} or type(signed["o"]) is not dict:
        raise Refused("cert_invalid")
    o = signed["o"]
    try:
        sig = unb64u(signed["sig"], S.SIG_LEN)
        ok = S.verify(pk_pub, sig, L["sig_device_cert"] + cj(o))
    except CanonError:
        ok = False
    if not ok or set(o) != DEVICE_CERT_FIELDS or o["v"] != 2 or o["suite"] != S.SUITE_ID or o["kind"] != "device_cert":
        raise Refused("cert_invalid")
    sc = o["scopes_max"]
    if (
        not _pub(o["dk_sig_pub"])
        or not _pub(o["dk_kx_pub"])
        or o["person_id"] != person_id(pk_pub)
        or o["device_id"] != device_id(unb64u(o["dk_sig_pub"], 65))
        or type(sc) is not list
        or not sc
        or sc != _LEVELS[: len(sc)]
        or "decide" not in sc
        or type(o["created_ms"]) is not int
        or o["created_ms"] > now_ms + CLOCK_SKEW_MS
    ):
        raise Refused("cert_invalid")
    exp = o["expires_ms"]
    if exp is not None and (type(exp) is not int or exp <= o["created_ms"]):
        raise Refused("cert_invalid")
    if exp is not None and now_ms >= exp:
        raise Refused("cert_expired")
    if o["device_id"] in revoked_devices:
        raise Refused("revoked")
    return o


def verify_approval(
    raw: Any,
    *,
    workspace_id: str,
    hub_id_: str,
    owner_person_id: str,
    request_hash: bytes,
    access: str,
    now_ms: int,
    revoked_devices: frozenset[str] = frozenset(),
) -> dict[str, Any]:
    """The owner's local-key approval carried by a hub-forwarded publish request (V3-D4)."""
    if type(raw) is not dict or set(raw) != {"o", "sig", "device_cert", "pk_pub"}:
        raise Refused("malformed")
    o = _envelope(raw, "hub.publish_approval", ("device_cert", "pk_pub"))
    if not _pub(raw["pk_pub"]):
        raise Refused("malformed")
    pk = unb64u(raw["pk_pub"], 65)
    if person_id(pk) != owner_person_id:
        raise Refused("other_person")
    cert = verify_device_cert(raw["device_cert"], pk, now_ms=now_ms, revoked_devices=revoked_devices)
    try:
        _signature(raw, L["sig_hub_publish_approval"], unb64u(cert["dk_sig_pub"], 65))
    except Refused as r:
        raise Refused("bad_approval" if r.code == "bad_signature" else r.code) from None
    _fields(o, "hub.publish_approval")
    if o["device_id"] != cert["device_id"]:
        raise Refused("bad_approval")
    if o["workspace_id"] != workspace_id:
        raise Refused("other_workspace")
    if o["hub_id"] != hub_id_:
        raise Refused("other_hub")
    if unb64u(o["request_hash"], 32) != request_hash or o["access"] != access:
        raise Refused("bad_approval")
    return o


_TS = re.compile(r"0|[1-9][0-9]{0,15}")
STATUS = {
    "malformed": 400,
    "stale_timestamp": 401,
    "unknown_workspace": 401,
    "bad_signature": 401,
    "not_bound": 403,
    "other_hub": 403,
    "unknown_hub": 401,
    "bad_hub_signature": 401,
    "replay": 401,
    "approval_required": 403,
    "bad_approval": 403,
    "other_person": 403,
    "other_workspace": 403,
    "cert_invalid": 400,
    "cert_expired": 400,
    "revoked": 403,
    "dev_refused": 403,
    "dev_mismatch": 403,
}


def check_direct_publish(req: Mapping[str, Any], wsk_pub: bytes) -> dict[str, Any]:
    """The v2 §12.1 direct path's signature step only (headers and WSK over the direct bytes). Used to show that a
    hub-path signature is never valid here (L2-2)."""
    h = {str(k).lower(): v for k, v in req["headers"].items()}
    try:
        ws = unhex(h["orch-workspace"], 16)
        if type(h["orch-ts"]) is not str or not _TS.fullmatch(h["orch-ts"]):
            raise CanonError("ts")
        ts, nonce, sig = int(h["orch-ts"]), unhex(h["orch-nonce"], 16), unb64u(h["orch-signature"], 64)
    except KeyError, CanonError:
        raise Refused("malformed") from None
    if not S.verify(
        wsk_pub, sig, publish_signed_bytes(ws, ts, nonce, req["method"], req["host"], req["target"], req["body"])
    ):
        raise Refused("bad_signature")
    return {"workspace": ws.hex()}


def check_hub_publish(req: Mapping[str, Any], st: Mapping[str, Any], *, now_ms: int, dev_mode: bool) -> dict[str, Any]:
    """orch-publish's check of a hub-forwarded request (contracts/hub/protocol.md §11), in order. ``req`` = {method, host,
    target, headers, body (bytes), access ("public" or None: what the request grants, as orch-publish's routes
    define)}. ``st`` = {cards: {ws: {wsk_pub, owner_person_id}}, bindings: {ws: binding state}, hub_cards: {hub_id:
    held card}, nonces: {ws: {nonce: until_ms}}, revoked_devices}. Mutates ``st["nonces"]`` on success of step 8."""
    h = {str(k).lower(): v for k, v in req["headers"].items()}
    # 1. header syntax
    try:
        ws = unhex(h["orch-workspace"], 16)
        if type(h["orch-ts"]) is not str or not _TS.fullmatch(h["orch-ts"]):
            raise CanonError("ts")
        ts = int(h["orch-ts"])
        nonce = unhex(h["orch-nonce"], 16)
        wsig = unb64u(h["orch-signature"], 64)
        hub = h["orch-hub"]
        unhex(hub, 16)
        hsig = unb64u(h["orch-hub-signature"], 64)
        approval = None
        if "orch-owner-approval" in h:
            approval = parse(unb64u(h["orch-owner-approval"]))
        for part in (req["method"], req["host"], req["target"]):
            if type(part) is not str or not part.isascii():
                raise CanonError("method, host and target are ASCII (L2-10)")
        if type(req["body"]) is not bytes:
            raise CanonError("body")
    except KeyError, CanonError, Refused:
        raise Refused("malformed") from None
    wsx = ws.hex()
    # 2. window
    if abs(now_ms - ts) > PUBLISH_WINDOW_MS:
        raise Refused("stale_timestamp", server_ms=now_ms)
    # 3. the workspace's card
    card = st["cards"].get(wsx)
    if card is None:
        raise Refused("unknown_workspace")
    # 4. WSK over the hub-path bytes: the request, this hub, and the approval exactly as attached (L2-2)
    pbytes = hub_path_signed_bytes(ws, ts, nonce, req["method"], req["host"], req["target"], req["body"], hub, approval)
    if not S.verify(unb64u(card["wsk_pub"], 65), wsig, pbytes):
        raise Refused("bad_signature")
    # 5. a live binding of this workspace to this hub
    b = st["bindings"].get(wsx)
    if b is None or b["status"] != "bound" or b["binding"]["wsk_pub"] != card["wsk_pub"]:
        raise Refused("not_bound")  # L2-10: only a binding under the card's current WSK counts
    if b["binding"]["hub_id"] != hub:
        raise Refused("other_hub")
    # 6. the hub's card, under the root the binding names
    held = st["hub_cards"].get(hub)
    if held is None or b64u(held["hk_root_pub"]) != b["binding"]["hk_root_pub"]:
        raise Refused("unknown_hub")
    # 7. the hub's transport signature
    if not S.verify(held["hk_op_pub"], hsig, hub_publish_bytes(hub, pbytes)):
        raise Refused("bad_hub_signature")
    # 8. nonce (after every signature, as §12.1)
    seen = st["nonces"].setdefault(wsx, {})
    for n, until in list(seen.items()):
        if until < now_ms:
            del seen[n]
    if nonce.hex() in seen:
        raise Refused("replay")
    seen[nonce.hex()] = now_ms + NONCE_RETENTION_MS
    # 9. the owner's approval (V3-D4): required for public access; verified whenever present
    if approval is None:
        if req.get("access") == "public":
            raise Refused("approval_required")
        return {"workspace": wsx, "hub": hub, "approved": False}
    verify_approval(
        approval,
        workspace_id=wsx,
        hub_id_=hub,
        owner_person_id=card["owner_person_id"],
        request_hash=publish_request_hash(ws, ts, nonce, req["method"], req["host"], req["target"], req["body"], hub),
        access=req.get("access") or "",
        now_ms=now_ms,
        revoked_devices=st.get("revoked_devices", frozenset()),
    )
    return {"workspace": wsx, "hub": hub, "approved": True}


# --- hub admin read for orch-publish (F3, §10.6) ---------------------------------------------------------------------

ADMIN_READ_WINDOW_MS = 120_000
_HEX32 = re.compile(r"[0-9a-f]{32}")
ADMIN_TARGET_MAX = 2048
#: The metadata an admin read may return per release, and nothing else (never content or secrets).
ADMIN_READ_ROW: dict[str, Check] = {
    "workspace_id": _hex16,
    "release_id": lambda x: type(x) is str and 0 < len(x) <= 64,
    "kind": lambda x: type(x) is str and 0 < len(x) <= 32,
    "state": lambda x: type(x) is str and 0 < len(x) <= 32,
    "created_ms": _int_min(0),
    "expires_ms": lambda x: x is None or _int_min(0)(x),
    "revoked": _bool,
    "bytes": _int_min(0),
    "public_url": lambda x: x is None,  # hv 0: orch-publish has no public kind; see protocol.md §10.6
}


ADMIN_READ_PATH = "/v2/hub/releases"


def parse_admin_target(target: Any) -> str | None:
    """The target of an admin read is exactly orch-publish's route ``GET|HEAD /v2/hub/releases[?workspace=<id>]``
    (F3), in canonical form, or it is ``malformed`` (L2P-5): ASCII, no ``%`` (no percent-encoding at all), the path
    byte for byte :data:`ADMIN_READ_PATH` (so no ``//``, ``.``, ``..``, trailing slash or upper case), and an optional
    query that is the single key ``workspace`` with 32 lower-case hex digits (no other key, no repeat, no ``#``,
    ``+``, ``;``). Returns the workspace the target names, or None (all workspaces in scope). The scope check applies
    to that workspace."""
    if type(target) is not str or not target.isascii() or len(target) > ADMIN_TARGET_MAX:
        raise Refused("malformed")
    path, sep, query = target.partition("?")
    if path != ADMIN_READ_PATH:
        raise Refused("malformed")
    if not sep:
        return None
    k, eq, v = query.partition("=")
    if k != "workspace" or not eq or not _HEX32.fullmatch(v):
        raise Refused("malformed")
    return v


def check_admin_rows(rows: list[Any], scope: list[str]) -> list[dict[str, Any]]:
    """Every row the data layer returns is checked against the scope after routing: a row of a workspace outside it
    is never emitted (``not_bound``), whatever the target resolved to (L2P-5)."""
    out = []
    for r in rows:
        check_admin_read_row(r)
        if r["workspace_id"] not in scope:
            raise Refused("not_bound")
        out.append(r)
    return out


def admin_read_bytes(hub_id_: str, ts_ms: int, method: str, target: str) -> bytes:
    """``label || suite || hub_id (16) || u64(ts_ms) || lp16(METHOD) || lp16(path + raw query)``. The raw query is
    signed as sent, so a swapped query or path verifies as ``bad_hub_signature``; the label keeps a publish
    transport signature from being read as an admin read."""
    if type(ts_ms) is not int or not 0 <= ts_ms < 1 << 64:
        raise Refused("malformed")
    for part in (method, target):
        if type(part) is not str or not part.isascii():
            raise Refused("malformed")
    return (
        L["sig_hub_admin_read"]
        + bytes([S.SUITE_ID])
        + unhex(hub_id_, 16)
        + struct.pack(">Q", ts_ms)
        + _lp16(method.encode("ascii"))
        + _lp16(target.encode("ascii"))
    )


def check_admin_read(req: Mapping[str, Any], st: Mapping[str, Any], *, now_ms: int, dev_mode: bool) -> dict[str, Any]:
    """orch-publish's check of a hub admin read (§10.6), in order. ``req`` = {method, target (path + raw query),
    headers}. ``st`` = {cards: {ws: {wsk_pub}}, bindings: {ws: binding state}, hub_cards: {hub_id: signed hub card},
    revoked_op_certs}. Returns the hub and the workspaces the read may cover. No nonce store, deliberately: the read
    is idempotent metadata, so a replay inside the 120 s window returns what the original did."""
    h = {str(k).lower(): v for k, v in req["headers"].items()}
    try:
        hub = h["orch-hub"]
        unhex(hub, 16)
        if type(h["orch-hub-ts"]) is not str or not _TS.fullmatch(h["orch-hub-ts"]):
            raise CanonError("ts")
        ts = int(h["orch-hub-ts"])
        sig = unb64u(h["orch-hub-signature"], 64)
        if req["method"] not in ("GET", "HEAD"):
            raise CanonError("method")
        named = parse_admin_target(req["target"])
        msg = admin_read_bytes(hub, ts, req["method"], req["target"])
    except KeyError, CanonError, Refused:
        raise Refused("malformed") from None
    if abs(now_ms - ts) > ADMIN_READ_WINDOW_MS:
        raise Refused("stale_timestamp", server_ms=now_ms)
    # The held card is verified again for this request: op cert valid and not revoked, hub_id of the root, dev marker.
    try:
        held = verify_card(
            st["hub_cards"][hub],
            now_ms=now_ms,
            dev_mode=dev_mode,
            revoked_op_certs=frozenset(st.get("revoked_op_certs", [])),
        )
    except KeyError, Refused:
        raise Refused("unknown_hub") from None
    if not S.verify(held["hk_op_pub"], sig, msg):
        raise Refused("bad_hub_signature")
    scope = sorted(
        ws
        for ws, b in st["bindings"].items()
        if b["status"] == "bound"
        and b["binding"]["mode"] == "full"
        and b["binding"]["hub_id"] == hub
        and b["binding"]["hk_root_pub"] == b64u(held["hk_root_pub"])
        and ws in st["cards"]
        and b["binding"]["wsk_pub"] == st["cards"][ws]["wsk_pub"]
    )
    if named is not None and named not in scope:
        raise Refused("not_bound")
    return {"hub": hub, "workspaces": scope}


def check_admin_read_row(row: Any) -> dict[str, Any]:
    """The only shape of a release row an admin read returns."""
    if (
        type(row) is not dict
        or set(row) != set(ADMIN_READ_ROW)
        or not all(ok(row[k]) for k, ok in ADMIN_READ_ROW.items())
    ):
        raise Refused("malformed")
    return row
