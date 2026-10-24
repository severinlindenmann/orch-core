"""Test-side port of the verifier half of the orch hub contract H0 (orch-hub ``docs/protocol.md``).

orch-core must never import hub code (D79), so this module re-implements, from the contract text, exactly the checks
the vectors in ``vectors/vectors_hub.json`` pin for the workspace side and for the fake hub: op certs, the hub card,
uk/channel certs, snapshots, binding and unbound with the binding store, the v2 card and the hub's join checks
(section 9.5), the ``hub.joined`` payload and link messages. It is built on core's own canonical JSON
(``orch.canon.jcs``), encodings and suite 2 (``orch.crypto``). Not covered here, on purpose: custodial events
(phase 4, C3) and the orch-publish side of section 11 (``check_hub_publish``), which core never verifies.
Every verifier raises :class:`Refused` with one code of section 14 and fails closed.
"""

from __future__ import annotations

import re
import struct
from collections.abc import Callable, Mapping
from typing import Any

from orch.canon.jcs import JcsError, loads_strict
from orch.canon.jcs import dumps as _dumps
from orch.crypto import suite as _suite
from orch.crypto.encoding import EncodingError, b64u, unb64u, unhex

CanonError = (JcsError, EncodingError)
SUITE_ID = _suite.SUITE_ID
SIG_LEN = _suite.SIG_LEN
sha256 = _suite.sha256


def cj(o: Any) -> bytes:
    return _dumps(o)


def valid_public_key(pub: object) -> bool:
    try:
        _suite.validate_public_key(pub)
    except _suite.CryptoError:
        return False
    return True


verify_sig = _suite.verify

HUB_LABELS = {
    "sig_hub_op_cert": "orch/v2/hub-op-cert|",
    "sig_hub_op_revoked": "orch/v2/hub-op-revoked|",
    "sig_hub_card": "orch/v2/hub-card|",
    "sig_hub_uk_cert": "orch/v2/hub-uk-cert|",
    "sig_hub_channel_cert": "orch/v2/hub-channel-cert|",
    "sig_hub_snapshot": "orch/v2/hub-snapshot|",
    "sig_hub_route": "orch/v2/hub-route|",
    "sig_hub_publish": "orch/v2/hub-publish|",
    "sig_hub_audit": "orch/v2/hub-audit|",
    "sig_hub_channel_proof": "orch/v2/hub-channel-proof|",
    "sig_hub_binding": "orch/v2/sig/hub-binding|",
    "sig_hub_unbound": "orch/v2/sig/hub-unbound|",
    "sig_hub_link": "orch/v2/sig/hub-link|",
    "sig_hub_link_auth": "orch/v2/sig/hub-link-auth|",
    "sig_publish_hub": "orch/v2/publish-hub|",
    "sig_hub_publish_approval": "orch/v2/sig/hub-publish-approval|",
    "sig_hub_owner_proof": "orch/v2/sig/hub-owner-proof|",
    "h_hub_id": "orch/v2/id/hub|",
    "h_channel_id": "orch/v2/id/channel|",
    "h_hub_event_digest": "orch/v2/hub-event-digest|",
    "h_hub_publish_request": "orch/v2/hub-publish-request|",
}
V2_LABELS = {
    "sig_ws_delegation": "orch/v2/sig/ws-delegation|",
    "sig_card_wsk": "orch/v2/sig/card-wsk|",
    "sig_publish": "orch/v2/publish|",
    "h_person_id": "orch/v2/id/person|",
    "h_device_id": "orch/v2/id/device|",
}
L: dict[str, bytes] = {k: v.encode("ascii") for k, v in {**HUB_LABELS, **V2_LABELS}.items()}


HV = 0  # hub contract version (H0)
CONTRACT_NAME = "orch.hub/0"
CLOCK_SKEW_MS = 300_000  # protocol-v2 §15: created/issued may be up to 300 s in the future
LINK_WINDOW_MS = 120_000
PUBLISH_WINDOW_MS = 120_000
NONCE_RETENTION_MS = 240_000
ROLES = ("read", "edit", "admin")
FACTORS = ("se-unlocked", "passkey", "password+totp")
MODES = ("selective", "full")


class Refused(Exception):
    def __init__(self, code: str, **extra: Any) -> None:
        super().__init__(code)
        self.code = code
        self.extra = extra


# --- ids ----------------------------------------------------------------------------------------------------------


def _key_hash(label: str, pub: bytes) -> bytes:
    if not valid_public_key(pub):
        raise Refused("malformed")
    return sha256(L[label] + bytes([SUITE_ID]) + pub)


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


def _hex16(x: Any) -> bool:
    try:
        unhex(x, 16)
        return True
    except CanonError:
        return False


def _pub(x: Any) -> bool:
    try:
        return valid_public_key(unb64u(x, 65))
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


_URL = re.compile(r"https://[a-z0-9.-]{1,253}(:[0-9]{1,5})?|http://(localhost|127\.0\.0\.1|\[::1\])(:[0-9]{1,5})?")


def _relay_url(x: Any) -> bool:
    return x is None or (type(x) is str and _URL.fullmatch(x) is not None)


HEADER = {"v": _const(2), "suite": _const(SUITE_ID), "kind": None, "hv": _const(HV)}

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
        "reader": lambda x: type(x) is int and 0 <= x <= 2**31 - 1,
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
    "hub.binding": "sig_hub_binding",
    "hub.unbound": "sig_hub_unbound",
    "hub.publish_approval": "sig_hub_publish_approval",
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
    if o.get("suite") != SUITE_ID or type(o.get("suite")) is not int:
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
        sig = unb64u(signed["sig"], SIG_LEN)
    except CanonError:
        raise Refused("malformed") from None
    if not verify_sig(pub, sig, signing_bytes(label, signed["o"])):
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
    if d["suite"] != SUITE_ID or o["suite"] != SUITE_ID:
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
        if b64u(sha256(unb64u(card["sealed"]))) != o["sealed_hash"]:
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


def check_joined_payload(p: Any, *, prior_aliases: Mapping[str, str] = {}) -> dict[str, str]:
    """The payload of the workspace-log event ``hub.joined`` (signed by the owner's device key like any person event,
    ``orch/v2/sig/ws-event|``). Returns the alias map person_id -> user_id after it. Aliases are never re-pointed.
    #1198 C8: the ``owner_proof`` member is required (``null`` is ``proof_missing``; verified by the production
    ``wire.check_recorded_owner_proof``)."""
    if type(p) is not dict or set(p) != {"hub_id", "hk_root_pub", "mode", "binding_seq", "aliases", "owner_proof"}:
        raise Refused("malformed")
    if p["owner_proof"] is None:
        raise Refused("proof_missing")
    if type(p["owner_proof"]) is not dict:
        raise Refused("malformed")
    if not (
        _pub(p["hk_root_pub"])
        and _hex16(p["hub_id"])
        and p["mode"] in MODES
        and _int_min(1)(p["binding_seq"])
        and type(p["aliases"]) is list
    ):
        raise Refused("malformed")
    if p["hub_id"] != hub_id(unb64u(p["hk_root_pub"], 65)):
        raise Refused("hub_id_mismatch")
    out = dict(prior_aliases)
    users = {u: pid for pid, u in prior_aliases.items()}
    pids = []
    for a in p["aliases"]:
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


# --- link messages ------------------------------------------------------------------------------------------------

#: op -> (direction of the request, mode it exists in or None for both, exact body fields)
LINK_OPS: dict[str, tuple[str, str | None, dict[str, Check]]] = {
    "snapshot_get": ("w2h", None, {}),
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
        },
    ),
    "card_put": ("w2h", None, {"card": lambda x: type(x) is dict, "pk_pub": _pub}),
    "binding_put": ("w2h", None, {"object": lambda x: type(x) is dict}),
    "audit_ack": ("w2h", None, {"seq": _int_min(1), "head": _b64u_n(32)}),
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
    "share_put": ("w2h", "selective", {"item_id": _hex16, "sealed": _b64u_any}),
    "share_revoke": ("w2h", "selective", {"item_id": _hex16}),
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
    if is_request and (set(o["body"]) != set(body) or not all(ok(o["body"][k]) for k, ok in body.items())):
        raise Refused("malformed")
    return o


def link_auth_bytes(hub_id_: str, workspace_id: str, challenge: bytes, exporter: bytes) -> bytes:
    """What WSK signs to open a link over TLS: the hub's 32-byte challenge bound to the 32-byte TLS exporter
    (label ``EXPORTER-orch-hub-link``, empty context)."""
    if len(challenge) != 32 or len(exporter) != 32:
        raise Refused("malformed")
    return (
        L["sig_hub_link_auth"] + bytes([SUITE_ID]) + unhex(hub_id_, 16) + unhex(workspace_id, 16) + challenge + exporter
    )


# --- publish through the hub: the bytes the hub checks and signs when it forwards (section 11.1) -------------------

_TS = re.compile(r"0|[1-9][0-9]{0,15}")
H_PUBLISH_REQUEST_LABEL = "h_hub_publish_request"


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
        + bytes([SUITE_ID])
        + ws
        + struct.pack(">Q", ts_ms)
        + nonce
        + _lp16(method.encode("ascii"))
        + _lp16(host.encode("ascii"))
        + _lp16(target.encode("ascii"))
        + sha256(body)
    )


def _request_fields(ws: bytes, ts_ms: int, nonce: bytes, method: str, host: str, target: str, body: bytes) -> bytes:
    """The v2 §12.1 fields after the label, in v2 order."""
    return (
        bytes([SUITE_ID])
        + ws
        + struct.pack(">Q", ts_ms)
        + nonce
        + _lp16(method.encode("ascii"))
        + _lp16(host.encode("ascii"))
        + _lp16(target.encode("ascii"))
        + sha256(body)
    )


def approval_hash(approval: Mapping[str, Any] | None) -> bytes:
    """``H(cj(approval))``, or 32 zero bytes when the request carries none."""
    if approval is None:
        return bytes(32)
    try:
        return sha256(cj(dict(approval)))
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
    return L["sig_hub_publish"] + bytes([SUITE_ID]) + unhex(hub_id_, 16) + sha256(wsk_bytes)


def check_left_payload(p: Any) -> dict[str, Any]:
    """``hub.left``: ``{hub_id, binding_seq, owner_proof}`` exactly (protocol 9.1, #1198 C8)."""
    if type(p) is not dict or set(p) != {"hub_id", "binding_seq", "owner_proof"}:
        raise Refused("malformed")
    if p["owner_proof"] is None:
        raise Refused("proof_missing")
    if type(p["owner_proof"]) is not dict:
        raise Refused("malformed")
    if not (_hex16(p["hub_id"]) and _int_min(1)(p["binding_seq"])):
        raise Refused("malformed")
    return {"hub_id": p["hub_id"], "binding_seq": p["binding_seq"]}
