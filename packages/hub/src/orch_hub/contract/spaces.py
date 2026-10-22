"""Shared spaces (protocol.md §10.4, V3-D8): the signed space record, the SK_e wrap to a member workspace, the sealed
item, and the hub's pure put / revoke / get rules. Pure functions, no I/O, no clock, no key storage.

Option B (spec D75): the hub holds every ``SK_e``, opens every item and may index it; a member host holds the ``SK_e``
of the epochs it was a member of. ``WSK``-signed items inside the seal tell readers (and a later reader of the hub's
store) who wrote them.
"""

from __future__ import annotations

import os
import struct
from collections.abc import Mapping
from typing import Any

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from . import suite as S
from .canon import CanonError, b64u, cj, loads_strict, unb64u, unhex
from .hub import (
    Refused,
    _dev,
    _envelope,
    _fields,
    _hex16,
    _signature,
    _under_card,
)
from .labels import KDF_SPACE_ITEM, KDF_SPACE_ITEM_MSG, PURPOSE_SPACE_SK, L

MAX_MEMBERS = 64
MAX_SEALED = 256 * 1024  # bytes of one sealed item (before b64u)
GET_PAGE = 100


def _hkdf(ikm: bytes, salt: bytes | None, info: bytes) -> bytes:
    return HKDF(algorithm=hashes.SHA256(), length=32, salt=salt or bytes(32), info=info).derive(ikm)


def _u32(n: int) -> bytes:
    return struct.pack(">I", n)


# --- SK_e commitment, item sealing ------------------------------------------------------------------------------------


def sk_commit(space_id: str, epoch: int, sk: bytes) -> str:
    """b64u(H("orch/v2/hub-sk-commit|" || suite || space_id || u32(epoch) || SK_e)): inside the signed space record, so
    every member opens a wrap to the same key and a wrong key is detected (like v2's ``ck_commit``)."""
    if len(sk) != 32:
        raise Refused("malformed")
    return b64u(S.sha256(L["h_sk_commit"] + bytes([S.SUITE_ID]) + unhex(space_id, 16) + _u32(epoch) + sk))


def members_hash(members: list[str]) -> str:
    """b64u(H("orch/v2/hub-space-members|" || cj(members))) of a sorted member list (L2P-1)."""
    try:
        return b64u(S.sha256(L["h_space_members"] + cj(list(members))))
    except CanonError:
        raise Refused("malformed") from None


def _item_aad(space_id: str, epoch: int, item_id: str) -> bytes:
    return L["aad_space"] + bytes([S.SUITE_ID]) + unhex(space_id, 16) + _u32(epoch) + unhex(item_id, 16)


def _item_key(sk: bytes, space_id: str, epoch: int, salt: bytes) -> bytes:
    base = _hkdf(sk, None, KDF_SPACE_ITEM + f"|{space_id}|{epoch}".encode())  # SK_e is never used directly
    return _hkdf(base, salt, KDF_SPACE_ITEM_MSG)


def seal_item(
    sk: bytes, space_id: str, epoch: int, item_id: str, signed_item: Mapping[str, Any], *, salt: bytes
) -> str:
    """SALTED-AEAD (v2 §5.1) of ``cj(signed space.item)``: ``salt (16) || AES-256-GCM(key, zero nonce, pt, AAD)``.
    ``salt`` is a fresh CSPRNG draw in real use; it is a parameter so the vectors are reproducible."""
    if len(salt) != 16:
        raise Refused("malformed")
    key = _item_key(sk, space_id, epoch, salt)
    ct = AESGCM(key).encrypt(bytes(12), cj(dict(signed_item)), _item_aad(space_id, epoch, item_id))
    return b64u(salt + ct)


def new_salt() -> bytes:
    return os.urandom(16)


def open_item(sk: bytes, space_id: str, epoch: int, item_id: str, sealed: str) -> dict[str, Any]:
    try:
        raw = unb64u(sealed)
    except CanonError:
        raise Refused("malformed") from None
    if len(raw) > MAX_SEALED:
        raise Refused("too_large")
    if len(raw) < 16 + 16:
        raise Refused("malformed")
    try:
        pt = AESGCM(_item_key(sk, space_id, epoch, raw[:16])).decrypt(
            bytes(12), raw[16:], _item_aad(space_id, epoch, item_id)
        )
        obj = loads_strict(pt)
    except InvalidTag, CanonError:
        raise Refused("malformed") from None  # wrong key, epoch, space or item id: the tag fails
    if type(obj) is not dict:
        raise Refused("malformed")
    return obj


# --- seal_to a workspace's WXK (v2 §5.3, purpose "space-sk") ---------------------------------------------------------


def _seal_aad(purpose: str, object_id: bytes, epoch: int, extra: bytes) -> bytes:
    p = purpose.encode()
    return L["aad_seal"] + bytes([S.SUITE_ID, len(p)]) + p + object_id + _u32(epoch) + extra


def _seal_key(ss: bytes, eph_pub: bytes, rcpt_pub: bytes, purpose: str, recipient_hex: str) -> bytes:
    return _hkdf(ss, eph_pub + rcpt_pub, b"orch/v2/seal|" + purpose.encode() + b"|" + recipient_hex.encode())


def _wrap_extra(hub_id: str, wxk_version: int) -> bytes:
    return unhex(hub_id, 16) + _u32(wxk_version)


def seal_sk(
    sk: bytes,
    *,
    hub_id: str,
    space_id: str,
    epoch: int,
    workspace_id: str,
    wxk_pub: bytes,
    wxk_version: int,
    eph_seed: bytes,
) -> str:
    """``sealed = eph_pub || AES-256-GCM(K, 0-nonce, SK_e, AAD)`` with the v2 §5.3 construction, purpose ``space-sk``,
    recipient id = ``workspace_id``, object = ``space_id``, epoch = the space epoch, extra = ``hub_id || u32(wxk_version)``
    (the wrap says which hub it is from and which WXK version it is for). ``eph_seed``: a fresh key per seal in real use."""
    eph = S.key_from_seed(eph_seed)
    eph_pub = S.public_bytes(eph)
    rcpt = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), wxk_pub)
    ss = eph.exchange(ec.ECDH(), rcpt)
    key = _seal_key(ss, eph_pub, wxk_pub, PURPOSE_SPACE_SK, workspace_id)
    aad = _seal_aad(PURPOSE_SPACE_SK, unhex(space_id, 16), epoch, _wrap_extra(hub_id, wxk_version))
    return b64u(eph_pub + AESGCM(key).encrypt(bytes(12), sk, aad))


def open_sk(
    sealed: str,
    *,
    hub_id: str,
    space_id: str,
    epoch: int,
    workspace_id: str,
    wxk_priv: ec.EllipticCurvePrivateKey,
    wxk_version: int,
) -> bytes:
    try:
        raw = unb64u(sealed)
        eph_pub, ct = raw[:65], raw[65:]
        if len(ct) != 32 + 16 or not S.valid_public_key(eph_pub):
            raise Refused("bad_wrap")
        ss = wxk_priv.exchange(ec.ECDH(), ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), eph_pub))
        if ss == bytes(32):
            raise Refused("bad_wrap")
        key = _seal_key(ss, eph_pub, S.public_bytes(wxk_priv), PURPOSE_SPACE_SK, workspace_id)
        aad = _seal_aad(PURPOSE_SPACE_SK, unhex(space_id, 16), epoch, _wrap_extra(hub_id, wxk_version))
        return AESGCM(key).decrypt(bytes(12), ct, aad)
    except InvalidTag, CanonError, ValueError:
        raise Refused("bad_wrap") from None


# --- the signed objects ----------------------------------------------------------------------------------------------


def _members(o: dict[str, Any]) -> list[str]:
    m = o["members"]
    if not m or len(m) > MAX_MEMBERS or any(not _hex16(w) for w in m) or m != sorted(set(m)):
        raise Refused("malformed")
    return m


def _identity(o: Mapping[str, Any]) -> tuple[Any, ...]:
    return (o["space_id"], o["epoch"], tuple(o["members"]), o["sk_commit"])


def verify_space(
    signed: Any,
    held: Mapping[str, Any],
    *,
    dev_mode: bool,
    prior: Mapping[str, Any] | None = None,
    revoked_op_certs: frozenset[str] = frozenset(),
) -> dict[str, Any]:
    """``hub.space`` under the held hub card's HK_op. ``prior`` is the record the host holds for this space. Epochs
    only go up (a host that polls may skip some); the same epoch with other bytes is ``space_conflict``."""
    o = _under_card(signed, "hub.space", held, dev_mode, revoked_op_certs)
    _members(o)
    if prior is not None:
        if o["space_id"] != prior["space_id"]:
            raise Refused("malformed")
        if o["epoch"] < prior["epoch"]:
            raise Refused("stale_epoch")
        if o["epoch"] == prior["epoch"]:
            # Identity is the content, not the signature or the op cert it names (L2P-3): after an HK_op rotation the hub
            # re-signs the current record, and that is not a conflict.
            if _identity(o) != _identity(prior):
                raise Refused("space_conflict")
            return dict(prior)
    return o


def verify_space_wrap(
    signed: Any,
    held: Mapping[str, Any],
    space: Mapping[str, Any],
    *,
    workspace_id: str,
    wxk_priv: ec.EllipticCurvePrivateKey,
    wxk_version: int,
    dev_mode: bool,
    revoked_op_certs: frozenset[str] = frozenset(),
) -> tuple[dict[str, Any], bytes]:
    """What a member host does with its wrap (already-verified ``space`` record of the same epoch): HK_op signature,
    the record names this workspace, the wrap is for this space, epoch, workspace and WXK version, the seal opens, and
    the key matches the record's ``sk_commit``. Returns ``(wrap object, SK_e)``."""
    o = _under_card(signed, "hub.space_wrap", held, dev_mode, revoked_op_certs)
    if o["space_id"] != space["space_id"] or o["epoch"] != space["epoch"]:
        raise Refused("malformed")
    if o["workspace_id"] != workspace_id:
        raise Refused("other_workspace")
    if workspace_id not in space["members"]:
        raise Refused("not_member")
    if o["wxk_version"] != wxk_version:
        raise Refused("bad_wxk_version")
    sk = open_sk(
        o["sealed"],
        hub_id=o["hub_id"],
        space_id=o["space_id"],
        epoch=o["epoch"],
        workspace_id=workspace_id,
        wxk_priv=wxk_priv,
        wxk_version=wxk_version,
    )
    if sk_commit(o["space_id"], o["epoch"], sk) != space["sk_commit"]:
        raise Refused("commit_mismatch")
    return o, sk


def verify_item(
    sealed: str,
    *,
    sk: bytes,
    space_id: str,
    epoch: int,
    item_id: str,
    members: list[str],
    wsk_pubs: Mapping[str, bytes],
    hub_id: str,
    dev_mode: bool,
) -> dict[str, Any]:
    """Open an item and check its author: the inner ``space.item`` is for this hub, space, epoch and item id, signs the
    hash of this epoch's member list (so it is the author's consent to exactly this set, L2P-1), its author was a
    member at this epoch (``members`` of that epoch) and its ``WSK`` signature verifies under the author's pinned key.
    ``wsk_pubs`` must come from the reader's own pins of the co-members' v2 cards, never from the hub (L2P-7)."""
    signed = open_item(sk, space_id, epoch, item_id, sealed)
    o = _envelope(signed, "space.item")
    _fields(o, "space.item")
    if o["hub_id"] != hub_id:
        raise Refused("other_hub")
    if o["space_id"] != space_id or o["item_id"] != item_id or o["epoch"] != epoch:
        raise Refused("malformed")
    if o["members_hash"] != members_hash(members):
        raise Refused("malformed")
    if o["workspace_id"] not in members:
        raise Refused("not_member")
    pub = wsk_pubs.get(o["workspace_id"])
    if pub is None:
        raise Refused("unknown_workspace")
    _signature(signed, L["sig_space_item"], pub)
    _dev(o, dev_mode)
    return o


# --- the hub's rules: put / revoke / get over one space ---------------------------------------------------------------
# State of one space: {"space": newest hub.space object, "history": [{"epoch", "members"}...] (oldest first),
#   "items": {item_id: {"seq", "epoch", "ws", "digest" (hex), "revoked"}}, "next_seq": int}.


def new_state(space: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "space": dict(space),
        "history": [{"epoch": space["epoch"], "members": list(space["members"])}],
        "items": {},
        "next_seq": 1,
    }


def apply_space(state: dict[str, Any], space: Mapping[str, Any]) -> dict[str, Any]:
    """A membership change: the hub's new record (higher epoch) becomes current; the history keeps who was a member of
    which epoch (needed for reads)."""
    if space["epoch"] <= state["space"]["epoch"]:
        raise Refused("stale_epoch")
    return {
        **state,
        "space": dict(space),
        "history": [*state["history"], {"epoch": space["epoch"], "members": list(space["members"])}],
    }


def _member(state: dict[str, Any] | None, ws: str) -> dict[str, Any]:
    # One answer for "no such space" and "not a member": a workspace learns nothing about spaces it is not in.
    if state is None or ws not in state["space"]["members"]:
        raise Refused("not_member")
    return state


def space_put(
    state: dict[str, Any] | None,
    ws: str,
    body: Mapping[str, Any],
    *,
    sks: Mapping[int, bytes],
    wsk_pubs: Mapping[str, bytes],
    dev_mode: bool,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """``ws`` is the link's workspace. Returns ``(new state, reply body)``. Order: membership, epoch, size, the item
    itself (the hub opens it under option B: author = the link's workspace, signed by its WSK), then replay."""
    st = _member(state, ws)
    epoch = st["space"]["epoch"]
    if body["epoch"] < epoch:
        raise Refused("stale_epoch")
    if body["epoch"] > epoch:
        raise Refused("malformed")
    if len(body["sealed"]) > MAX_SEALED * 4 // 3 + 4:
        raise Refused("too_large")
    held = st["items"].get(body["item_id"])
    digest = S.sha256(body["sealed"].encode()).hex()
    if held is not None:
        if held["digest"] != digest or held["ws"] != ws:
            raise Refused("replay")
        return st, {"seq": held["seq"]}  # the same put again: idempotent
    o = verify_item(
        body["sealed"],
        sk=sks[epoch],
        space_id=st["space"]["space_id"],
        epoch=epoch,
        item_id=body["item_id"],
        members=st["space"]["members"],
        wsk_pubs=wsk_pubs,
        hub_id=st["space"]["hub_id"],
        dev_mode=dev_mode,
    )
    if o["workspace_id"] != ws:
        raise Refused("other_workspace")  # an item is only put by the workspace that wrote and signed it
    seq = st["next_seq"]
    items = {**st["items"], body["item_id"]: {"seq": seq, "epoch": epoch, "ws": ws, "digest": digest, "revoked": False}}
    return {**st, "items": items, "next_seq": seq + 1}, {"seq": seq}


def space_revoke(
    state: dict[str, Any] | None, ws: str, body: Mapping[str, Any]
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Only the author workspace revokes its item, and only while a member. The revocation is a new ``seq`` so readers
    that poll see it. What was already read stays read."""
    st = _member(state, ws)
    held = st["items"].get(body["item_id"])
    if held is None:
        raise Refused("unknown_item")
    if held["ws"] != ws:
        raise Refused("other_workspace")
    if held["revoked"]:
        return st, {"seq": held["seq"]}
    seq = st["next_seq"]
    items = {**st["items"], body["item_id"]: {**held, "seq": seq, "revoked": True}}
    return {**st, "items": items, "next_seq": seq + 1}, {"seq": seq}


def space_get(state: dict[str, Any] | None, ws: str, body: Mapping[str, Any]) -> dict[str, Any]:
    """The entries (items and revocations) after ``since`` that ``ws`` may read: only items of epochs in which it was a
    member (a leaver gets nothing at all; a later joiner gets nothing from before it joined, there is no backfill).
    Returns ``{"entries": [{"seq", "item_id", "epoch", "revoked"}], "next"}``, at most :data:`GET_PAGE`."""
    st = _member(state, ws)
    mine = {h["epoch"] for h in st["history"] if ws in h["members"]}
    rows = sorted((v["seq"], k, v) for k, v in st["items"].items() if v["seq"] > body["since"] and v["epoch"] in mine)[
        :GET_PAGE
    ]
    entries = [{"seq": s, "item_id": k, "epoch": v["epoch"], "revoked": v["revoked"]} for s, k, v in rows]
    return {"entries": entries, "next": entries[-1]["seq"] if entries else body["since"]}


def apply_unbind(state: dict[str, Any] | None, ws: str, space: Mapping[str, Any] | None) -> dict[str, Any] | None:
    """A workspace's ``hub.unbound`` removes it from every space it is in, in the same step: ``space`` must be the
    hub's next record (epoch + 1, the old members minus ``ws``, a fresh ``SK_e`` by construction). A workspace that is
    not a member needs no record (``space`` is None, nothing changes). A record that still lists ``ws`` or changes
    anything else is ``malformed``."""
    if state is None or ws not in state["space"]["members"]:
        return state
    old = state["space"]
    if old["members"] == [ws]:
        return None  # the last member left: the space ends (audited as "delete"); its items go with it
    if (
        space is None
        or space["space_id"] != old["space_id"]
        or space["epoch"] != old["epoch"] + 1
        or space["members"] != [m for m in old["members"] if m != ws]
    ):
        raise Refused("malformed")
    return apply_space(state, space)


# --- consent (L2P-2) ---------------------------------------------------------------------------------------------------


def check_host_put(rule: Mapping[str, Any], space: Mapping[str, Any]) -> None:
    """Host side, before EVERY ``space_put``. ``rule`` = the owner's share rule ``{"space_id", "members"}``: the space
    AND the member set the owner approved. ``space`` = the current ``hub.space`` the host just verified (signature,
    epoch not older than the one it holds). If the set holds anyone the owner did not approve (a member was added), the
    host does not put: ``members_changed`` (local, never sent). A smaller set needs nothing. The owner re-approves a
    grown set at the host terminal, which replaces the rule."""
    if space["space_id"] != rule["space_id"]:
        raise Refused("malformed")
    if not set(space["members"]) <= set(rule["members"]):
        raise Refused("members_changed")


def check_added_members(
    old_members: list[str] | None,
    space: Mapping[str, Any],
    *,
    modes: Mapping[str, str | None],
    accepts: Mapping[str, Any],
    wsk_pubs: Mapping[str, bytes],
    dev_mode: bool,
) -> None:
    """Hub side, before it signs a ``hub.space`` that lists a workspace that was not listed before. A full-mode
    workspace is added by the hub-side authority (admin in every member workspace). A **selective** workspace (or one
    whose mode is unknown) is added only with its own ``space.accept``, signed by its ``WSK`` (made by the owner's
    human-only host verb) for exactly this transition: the epoch the addition creates, the member set before it
    (``prev_members_hash``; the empty list for a new space) and the set after it, so an old accept cannot be replayed
    when the same set recurs (a workspace removed and later re-added needs a new owner decision); a hub-admin can remove it, never add it (D71, D80)."""
    for ws in space["members"]:
        if old_members is not None and ws in old_members:
            continue
        mode = modes.get(ws)
        if mode is None:
            raise Refused("unknown_workspace")
        if mode == "full":
            continue
        signed = accepts.get(ws)
        if signed is None:
            raise Refused("not_accepted")
        o = _envelope(signed, "space.accept")
        pub = wsk_pubs.get(ws)
        if pub is None:
            raise Refused("unknown_workspace")
        _signature(signed, L["sig_space_accept"], pub)
        _fields(o, "space.accept")
        _dev(o, dev_mode)
        if (
            o["hub_id"] != space["hub_id"]
            or o["space_id"] != space["space_id"]
            or o["workspace_id"] != ws
            or o["epoch"] != space["epoch"]
            or o["prev_members_hash"] != members_hash(old_members if old_members is not None else [])
            or o["members_hash"] != members_hash(space["members"])
        ):
            raise Refused("not_accepted")
