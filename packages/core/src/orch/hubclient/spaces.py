"""Shared spaces on the workspace host (hub contract section 10.4, V3-D8), from the text and the hub's reference rules.

Pure functions, every check fails closed with :class:`HubRefused`:

* :func:`verify_space` / :func:`verify_space_wrap`: the hub's signed records under the held hub card; the wrap opens
  with the workspace's ``WXK`` (purpose ``space-sk``) and must match the record's ``sk_commit``.
* :func:`build_item` / :func:`seal_item` / :func:`verify_item`: a ``space.item`` signed with ``WSK`` over hub, space,
  epoch and the member-set hash (L2P-1), sealed under a key derived from ``SK_e``. A reader takes the author's ``WSK``
  only from ITS OWN pins (``wsk_pubs``), never from the hub (L2P-7); an author without a pin is refused.
* :func:`check_host_put`: the owner's share rule pins ``{space_id, members}``; a grown member set is
  ``members_changed`` (local, never sent) (L2P-2).
* :func:`build_accept`: the ``space.accept`` a selective workspace's owner signs for exactly one transition (L2P-2b).

Items are untrusted input from other workspaces: data, never instructions."""

from __future__ import annotations

import os
import struct
from collections.abc import Callable, Mapping
from typing import Any

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.asymmetric.ec import EllipticCurvePrivateKey

from orch.canon.jcs import JcsError, loads_strict
from orch.crypto import suite
from orch.crypto.encoding import EncodingError, b64u, unb64u, unhex
from orch.crypto.labels import L
from orch.crypto.sealing import open_sealed

from . import wire
from .errors import HubRefused

__all__ = [
    "GET_PAGE",
    "MAX_FILE",
    "MAX_MEMBERS",
    "MAX_SEALED",
    "build_accept",
    "build_item",
    "check_host_put",
    "item_aad",
    "members_hash",
    "open_item",
    "seal_item",
    "sk_commit",
    "verify_item",
    "verify_space",
    "verify_space_wrap",
]

MAX_MEMBERS = 64
MAX_SEALED = 256 * 1024
GET_PAGE = 100
MAX_FILE = 190 * 1024  # bytes of one shared file: its sealed item stays under MAX_SEALED
SIG_SPACE = b"orch/v2/hub-space|"
SIG_SPACE_WRAP = b"orch/v2/hub-space-wrap|"
_KDF_ITEM = b"orch/v2/space-item"
_KDF_ITEM_MSG = b"orch/v2/space-item-msg"
Sign = Callable[[bytes], bytes]


def _u32(n: int) -> bytes:
    return struct.pack(">I", n)


def sk_commit(space_id: str, epoch: int, sk: bytes) -> str:
    if len(sk) != 32:
        raise HubRefused("malformed")
    try:
        return b64u(suite.sha256(L["h_sk_commit"] + bytes([suite.SUITE_ID]) + unhex(space_id, 16) + _u32(epoch) + sk))
    except (EncodingError, struct.error):
        raise HubRefused("malformed") from None


def members_hash(members: list[str]) -> str:
    try:
        return b64u(suite.sha256(L["h_space_members"] + wire.cj(list(members))))
    except wire._CanonError:  # noqa: SLF001
        raise HubRefused("malformed") from None


def item_aad(space_id: str, epoch: int, item_id: str) -> bytes:
    try:
        return L["aad_space"] + bytes([suite.SUITE_ID]) + unhex(space_id, 16) + _u32(epoch) + unhex(item_id, 16)
    except (EncodingError, struct.error):
        raise HubRefused("malformed") from None


def _base(sk: bytes, space_id: str, epoch: int) -> bytes:
    return suite.hkdf(sk, b"", _KDF_ITEM + f"|{space_id}|{epoch}".encode())  # SK_e itself is never an AEAD key


def seal_item(sk: bytes, space_id: str, epoch: int, item_id: str, signed_item: Mapping[str, Any]) -> str:
    """SALTED-AEAD of ``cj(signed space.item)`` with a fresh salt, the AAD binding space, epoch and item id."""
    blob = suite.salted_seal(
        _base(sk, space_id, epoch), _KDF_ITEM_MSG, item_aad(space_id, epoch, item_id), wire.cj(dict(signed_item))
    )
    if len(blob) > MAX_SEALED:
        raise HubRefused("too_large")
    return b64u(blob)


def open_item(sk: bytes, space_id: str, epoch: int, item_id: str, sealed: str) -> dict[str, Any]:
    try:
        raw = unb64u(sealed)
    except EncodingError:
        raise HubRefused("malformed") from None
    if len(raw) > MAX_SEALED:
        raise HubRefused("too_large")
    try:
        pt = suite.salted_open(_base(sk, space_id, epoch), _KDF_ITEM_MSG, item_aad(space_id, epoch, item_id), raw)
        obj = loads_strict(pt)
    except (InvalidTag, JcsError):
        raise HubRefused("malformed") from None  # wrong key, epoch, space or item id: the tag fails
    if type(obj) is not dict:
        raise HubRefused("malformed")
    return obj


def build_item(
    *, hub_id: str, space_id: str, epoch: int, members: list[str], workspace_id: str, body: dict[str, Any],
    created_ms: int, dev: bool, sign: Sign, item_id: str | None = None,
) -> tuple[str, dict[str, Any]]:  # fmt: skip
    """``(item_id, signed space.item)``: signed with ``WSK`` (custody) over hub, space, epoch and the member set."""
    item_id = item_id or os.urandom(16).hex()
    o = {
        "v": 2, "suite": suite.SUITE_ID, "kind": "space.item", "hv": wire.HV, "hub_id": hub_id, "space_id": space_id,
        "epoch": epoch, "members_hash": members_hash(members), "item_id": item_id, "workspace_id": workspace_id,
        "created_ms": created_ms, "body": body, "dev": dev,
    }  # fmt: skip
    signed = {"o": o, "sig": b64u(sign(L["sig_space_item"] + wire.cj(o)))}
    wire._fields(o, "space.item")  # noqa: SLF001 - never sign what the contract would refuse
    return item_id, signed


def build_accept(
    *, hub_id: str, space_id: str, workspace_id: str, epoch: int, prev_members: list[str], members: list[str],
    dev: bool, sign: Sign,
) -> dict[str, Any]:  # fmt: skip
    """The owner's consent to ONE transition: the epoch the addition creates, the member set before it and after it."""
    o = {
        "v": 2, "suite": suite.SUITE_ID, "kind": "space.accept", "hv": wire.HV, "hub_id": hub_id,
        "space_id": space_id, "workspace_id": workspace_id, "epoch": epoch,
        "prev_members_hash": members_hash(prev_members), "members_hash": members_hash(members), "dev": dev,
    }  # fmt: skip
    wire._fields(o, "space.accept")  # noqa: SLF001
    return {"o": o, "sig": b64u(sign(L["sig_space_accept"] + wire.cj(o)))}


# --- the hub's records ------------------------------------------------------------------------------------------------


def _under_card(signed: Any, kind: str, label: bytes, held: Mapping[str, Any], dev_mode: bool, revoked: frozenset[str]):
    if held["op_cert"]["cert_id"] in revoked:
        raise HubRefused("revoked")
    o = wire._envelope(signed, kind)  # noqa: SLF001
    wire._signature(signed, label, held["hk_op_pub"])  # noqa: SLF001
    wire._fields(o, kind)  # noqa: SLF001
    if o["hub_id"] != held["card"]["hub_id"]:
        raise HubRefused("other_hub")
    if o["op_cert_id"] != held["op_cert"]["cert_id"]:
        raise HubRefused("stale_op_cert")
    wire.check_dev(o["dev"], dev_mode)
    return o


def _members(o: Mapping[str, Any]) -> list[str]:
    m = o["members"]
    if not m or len(m) > MAX_MEMBERS or any(not wire.hex16(w) for w in m) or m != sorted(set(m)):
        raise HubRefused("malformed")
    return list(m)


def _identity(o: Mapping[str, Any]) -> tuple[Any, ...]:
    return (o["space_id"], o["epoch"], tuple(o["members"]), o["sk_commit"])


def verify_space(
    signed: Any, held: Mapping[str, Any], *, dev_mode: bool, prior: Mapping[str, Any] | None = None,
    revoked_op_certs: frozenset[str] = frozenset(),
) -> dict[str, Any]:  # fmt: skip
    """``hub.space`` under the held card's HK_op. ``prior`` is the record the host holds: epochs only go up
    (``stale_epoch``); the same epoch with other content is ``space_conflict`` (the identity is the content, not the
    signature, so a re-signing after an HK_op rotation is no conflict)."""
    o = _under_card(signed, "hub.space", SIG_SPACE, held, dev_mode, revoked_op_certs)
    _members(o)
    if prior is not None:
        if o["space_id"] != prior["space_id"]:
            raise HubRefused("malformed")
        if o["epoch"] < prior["epoch"]:
            raise HubRefused("stale_epoch")
        if o["epoch"] == prior["epoch"]:
            if _identity(o) != _identity(prior):
                raise HubRefused("space_conflict")
            return dict(prior)
    return o


def verify_space_wrap(
    signed: Any, held: Mapping[str, Any], space: Mapping[str, Any], *, workspace_id: str,
    wxk_priv: EllipticCurvePrivateKey, wxk_version: int, dev_mode: bool, revoked_op_certs: frozenset[str] = frozenset(),
) -> tuple[dict[str, Any], bytes]:  # fmt: skip
    """The member host's wrap: HK_op signature, this space and epoch, this workspace (listed) and WXK version, the
    seal opens, the key matches the record's ``sk_commit``. Returns ``(wrap object, SK_e)``."""
    o = _under_card(signed, "hub.space_wrap", SIG_SPACE_WRAP, held, dev_mode, revoked_op_certs)
    if o["space_id"] != space["space_id"] or o["epoch"] != space["epoch"]:
        raise HubRefused("malformed")
    if o["workspace_id"] != workspace_id:
        raise HubRefused("other_workspace")
    if workspace_id not in space["members"]:
        raise HubRefused("not_member")
    if o["wxk_version"] != wxk_version:
        raise HubRefused("bad_wxk_version")
    try:
        blob = unb64u(o["sealed"])
        sk = open_sealed(
            wxk_priv, unhex(workspace_id, 16), "space-sk", unhex(o["space_id"], 16), o["epoch"], blob,
            unhex(o["hub_id"], 16) + _u32(wxk_version),
        )  # fmt: skip
    except (InvalidTag, EncodingError, suite.CryptoError, struct.error, ValueError):
        raise HubRefused("bad_wrap") from None
    if len(sk) != 32 or sk_commit(o["space_id"], o["epoch"], sk) != space["sk_commit"]:
        raise HubRefused("commit_mismatch")
    return o, sk


def verify_item(
    sealed: str, *, sk: bytes, space_id: str, epoch: int, item_id: str, members: list[str],
    wsk_pubs: Mapping[str, bytes], hub_id: str, dev_mode: bool,
) -> dict[str, Any]:  # fmt: skip
    """Open an item and check its author. ``wsk_pubs`` are the reader's OWN pins of co-members' cards; an author
    with no pin is refused (``unknown_workspace``: author unverified)."""
    signed = open_item(sk, space_id, epoch, item_id, sealed)
    o = wire._envelope(signed, "space.item")  # noqa: SLF001
    wire._fields(o, "space.item")  # noqa: SLF001
    if o["hub_id"] != hub_id:
        raise HubRefused("other_hub")
    if o["space_id"] != space_id or o["item_id"] != item_id or o["epoch"] != epoch:
        raise HubRefused("malformed")
    if o["members_hash"] != members_hash(members):
        raise HubRefused("malformed")
    if o["workspace_id"] not in members:
        raise HubRefused("not_member")
    pub = wsk_pubs.get(o["workspace_id"])
    if pub is None:
        raise HubRefused("unknown_workspace")
    wire._signature(signed, L["sig_space_item"], pub)  # noqa: SLF001
    wire.check_dev(o["dev"], dev_mode)
    return o


def check_host_put(rule: Mapping[str, Any], space: Mapping[str, Any]) -> None:
    """Before EVERY ``space_put``: ``rule`` = the owner's ``{space_id, members}``, ``space`` = the verified current
    record. A member the owner did not approve is ``members_changed`` (local, never sent); a smaller set is fine."""
    if space["space_id"] != rule["space_id"]:
        raise HubRefused("malformed")
    if not set(space["members"]) <= set(rule["members"]):
        raise HubRefused("members_changed")
