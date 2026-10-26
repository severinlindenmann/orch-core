"""Pure builders for R6 membership (design 11A.15.10, H12c items 6 and 7): the member list object and its check, the
sealed ``WK`` of a grant, the grant object and its check, and the card-key wrap.

Specs: ``contracts/relay/protocol-v2.md`` §5.3 (sealing), §7.1 (card keys), §7.2 (member list), §7.3 (WK grants).
Vectors: the relay corpus sections ``member_lists``, ``member_list_snapshots``, ``wk_grants``, ``seal``,
``seal_open`` and ``card.card_key_wrap_to_phone`` (``tests/identity/test_memberlist.py``).

No I/O, no clock, no key held: the builders return the unsigned object ``o`` (R6 signs it through
``RelaySigner.member_list`` / ``RelaySigner.wk_grant``, 11A.15.7) and the checks take the public key to verify with.
Every function validates its inputs exactly and raises :class:`Refused`; a builder's refusal is ``malformed``, a
check's refusal is the protocol's code (``bad_signature``, ``malformed``, ``other_workspace``,
``not_for_this_device``). Sealing draws its ephemeral key from the CSPRNG inside ``orch.crypto.seal``; there is no
ephemeral parameter (the vectors fix it through a test-only seam). Keys and secrets are raw bytes, ids are 32
lower-case hex characters, and no secret (``WK``, ``CK``) ever appears in a message.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from orch import canon, crypto

from .certs import SCOPE_ORDER
from .errors import Refused
from .pairing import MAX_EPOCH, _bad, _id, _pub, _raw, _verify_signed

__all__ = [
    "FIRST_EPOCH_MAX",
    "GRANT_FIELDS",
    "MAX_MEMBERS",
    "MEMBER_FIELDS",
    "MEMBER_LIST_FIELDS",
    "SEALED_KEY_LEN",
    "Grant",
    "MemberEntry",
    "MemberList",
    "card_key_wrap",
    "check_member_list",
    "check_wk_grant",
    "member_list_o",
    "seal_wk",
    "wk_grant_o",
]

MAX_SAFE_INT = 2**53 - 1
FIRST_EPOCH_MAX = 2**16  # §7.2 check 4: the first list's epoch is in 1 .. 2^16
MAX_MEMBERS = 1024  # §7.2 P3-D7: the relay refuses a larger list
MEMBER_LIST_FIELDS = frozenset(
    {"v", "suite", "kind", "workspace_id", "epoch", "list_seq", "issued_ms", "wk_commit", "persons", "members"}
)
MEMBER_FIELDS = frozenset({"device_id", "person_id", "scopes"})
GRANT_FIELDS = frozenset({"v", "suite", "kind", "workspace_id", "device_id", "epoch", "sealed"})
SEALED_KEY_LEN = crypto.PUB_LEN + 32 + 16  # eph_pub || AES-GCM(32-byte key) || tag: a sealed WK or CK


def _epoch(epoch: object) -> int:
    if type(epoch) is not int or not 1 <= epoch <= MAX_EPOCH:
        raise _bad("epoch")
    return epoch


def _safe(value: object, low: int, what: str) -> int:
    if type(value) is not int or not low <= value <= MAX_SAFE_INT:
        raise _bad(what)
    return value


def _commit(value: object) -> str:
    try:
        crypto.unb64u(value, 32)
    except crypto.EncodingError:
        raise _bad("wk_commit") from None
    return value  # type: ignore[return-value]


# --- 6. the member list (§7.2) ---------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class MemberEntry:
    """One ``members`` entry: ``{device_id, person_id, scopes}``."""

    device_id: str
    person_id: str
    scopes: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class MemberList:
    """A member list that passed :func:`check_member_list`. ``digest`` is ``b64u(H(cj(o)))``, the device's D1
    ``list_hash`` (content identity: a high-s twin of the signature has the same digest). ``persons`` is exactly the
    list's field (the persons **besides** the card owner, §7.2 check 8)."""

    workspace_id: str
    epoch: int
    list_seq: int
    issued_ms: int
    wk_commit: str
    persons: tuple[str, ...]
    members: tuple[MemberEntry, ...]
    digest: str


def _scopes(value: object) -> list[str]:
    """A list entry's scopes as R6 writes them: a non-empty prefix of ``look, decide, operate, type`` (never a
    ``drop:`` scope: scoped agents are not list members, §7.2 check 8)."""
    if type(value) not in (list, tuple) or not value or list(value) != SCOPE_ORDER[: len(value)]:
        raise _bad("scopes")
    return list(value)


def member_list_o(
    workspace_id: str,
    epoch: int,
    list_seq: int,
    issued_ms: int,
    wk_commit: str,
    members: Iterable[Mapping[str, Any]],
    *,
    owner_person_id: str,
    first: bool,
) -> dict[str, Any]:
    """The unsigned §7.2 list ``o``: ``members`` sorted by ``device_id`` (unique), ``persons`` **derived** as the sorted
    set of the members' persons other than the card owner (``owner_person_id``), as the relay corpus has it (an
    owner-only list carries ``persons: []``; §7.2 check 8 admits the owner without listing it). So no person is
    ever listed without a member. Each member is exactly ``{device_id, person_id, scopes}`` with ``scopes`` a
    non-empty prefix of ``look, decide, operate, type``. ``wk_commit`` is :func:`orch.identity.pairing.wk_commit`'s
    output for this ``(workspace, epoch, WK)``.

    ``first`` (the workspace's first publication, §7.2 check 4): the list must hold at least one device of the owner
    and ``epoch <= 2^16``. Otherwise an empty list is valid (the empty rotation of 11A.15.4)."""
    ws = crypto.hex_id(_id(workspace_id, "workspace_id"))
    owner = crypto.hex_id(_id(owner_person_id, "owner_person_id"))
    if type(first) is not bool:
        raise _bad("first")
    _epoch(epoch)
    _safe(list_seq, 1, "list_seq")
    _safe(issued_ms, 0, "issued_ms")
    commit = _commit(wk_commit)
    if isinstance(members, (str, bytes, Mapping)):
        raise _bad("members")
    entries = []
    for m in members:
        if not isinstance(m, Mapping) or set(m) != MEMBER_FIELDS:
            raise _bad("member field set")
        entries.append(
            {
                "device_id": crypto.hex_id(_id(m["device_id"], "device_id")),
                "person_id": crypto.hex_id(_id(m["person_id"], "person_id")),
                "scopes": _scopes(m["scopes"]),
            }
        )
    entries.sort(key=lambda e: e["device_id"])
    ids = [e["device_id"] for e in entries]
    if len(set(ids)) != len(ids):
        raise _bad("duplicate device_id")
    if len(entries) > MAX_MEMBERS:
        raise _bad("too many members")
    if first and (epoch > FIRST_EPOCH_MAX or not any(e["person_id"] == owner for e in entries)):
        raise _bad("first list: epoch above 2^16 or no device of the owner")
    return {
        "v": 2,
        "suite": crypto.SUITE_ID,
        "kind": "member_list",
        "workspace_id": ws,
        "epoch": epoch,
        "list_seq": list_seq,
        "issued_ms": issued_ms,
        "wk_commit": commit,
        "persons": sorted({e["person_id"] for e in entries} - {owner}),
        "members": entries,
    }


def _sorted_unique_ids(values: object, what: str) -> list[str]:
    if type(values) is not list:
        raise _bad(what)
    for v in values:
        _id(v, what)
    if values != sorted(set(values)):
        raise _bad(what)
    return values


def check_member_list(signed: object, wsk_pub: bytes, workspace_id: str) -> MemberList:
    """§7.2 checks 1 to 3 as a device runs them (the relay reference's ``_member_list_shape`` and its order): the
    ``WSK`` signature (``bad_signature``), then the exact shape (``malformed``: field sets, sorted unique hex ids,
    ``1 <= epoch <= 2^32-1``, ``1 <= list_seq <= 2^53-1``, ``wk_commit`` canonical b64u of 32 bytes; also at most
    1024 members and each ``scopes`` a list of strings), then ``workspace_id`` (``other_workspace``).

    It does **not** decide currency (``stale_list``, ``bad_epoch``, ``rotation_required``, ``wk_commit_changed``, the
    D2 alarms) or certificates and grants (checks 8 and 9): those need the caller's previous list and directory.

    **Unvalidated beyond shape (Opus L4):** ``MemberEntry.scopes`` is a list of strings, not checked as a scope
    prefix (``[]``, a ``drop:`` scope or repeats pass); ``persons`` may name the owner or a person without a member, and
    a member's person need not be the owner or listed. The relay owns those checks (8 and the persons rule). So R6
    never takes ``answer_approved``'s ``scopes`` (or any authority) from a fetched list: it uses the offer's scopes and
    its own exposed body (H2/H7)."""
    wsk = _pub(wsk_pub, "wsk_pub")
    ws = crypto.hex_id(_id(workspace_id, "workspace_id"))
    o = _verify_signed(wsk, signed, "member_list")
    if o is None:
        raise Refused("bad_signature", "member_list")
    if set(o) != MEMBER_LIST_FIELDS:
        raise Refused("malformed", "member_list field set")
    try:
        members = o["members"]
        if type(members) is not list or len(members) > MAX_MEMBERS:
            raise _bad("members")
        entries = []
        for m in members:
            if type(m) is not dict or set(m) != MEMBER_FIELDS:
                raise _bad("member field set")
            _id(m["device_id"], "device_id")
            _id(m["person_id"], "person_id")
            if type(m["scopes"]) is not list or any(type(s) is not str for s in m["scopes"]):
                raise _bad("scopes")
            entries.append(MemberEntry(m["device_id"], m["person_id"], tuple(m["scopes"])))
        persons = _sorted_unique_ids(o["persons"], "persons")
        _sorted_unique_ids([e.device_id for e in entries], "device_id order")
        _epoch(o["epoch"])
        _safe(o["list_seq"], 1, "list_seq")
        _commit(o["wk_commit"])
        _safe(o["issued_ms"], 0, "issued_ms")
    except Refused as e:
        raise Refused("malformed", e.detail) from None
    if o["workspace_id"] != ws:
        raise Refused("other_workspace", "member_list")
    return MemberList(
        workspace_id=ws,
        epoch=o["epoch"],
        list_seq=o["list_seq"],
        issued_ms=o["issued_ms"],
        wk_commit=o["wk_commit"],
        persons=tuple(persons),
        members=tuple(entries),
        digest=crypto.b64u(crypto.sha256(canon.dumps(o))),
    )


# --- 7. WK grants and card-key wraps (§5.3, §7.1, §7.3) -------------------------------------------------------------


def _seal_key(purpose: str, dk_kx_pub: bytes, device_id: str, workspace_id: str, epoch: int, key: bytes) -> bytes:
    kx = _pub(dk_kx_pub, "dk_kx_pub")
    dev = _id(device_id, "device_id")
    ws = _id(workspace_id, "workspace_id")
    k = _raw(key, 32, "key")
    try:
        return crypto.seal(kx, dev, purpose, ws, epoch, k)
    except crypto.CryptoError:
        raise _bad("seal") from None


def seal_wk(dk_kx_pub: bytes, device_id: str, workspace_id: str, epoch: int, wk: bytes) -> bytes:
    """``seal_to(dk_kx, purpose "wk", recipient = device_id, object = workspace_id, epoch, WK_epoch)`` (§5.3, §7.3):
    ``eph_pub || ct`` (113 bytes), a fresh ephemeral key per call. ``device_id`` is the recipient's own id; R6 passes
    the id it allocated for these keys (``device_id(dk_sig_pub)``), never one read from the relay."""
    return _seal_key("wk", dk_kx_pub, device_id, workspace_id, _epoch(epoch), wk)


def card_key_wrap(dk_kx_pub: bytes, device_id: str, workspace_id: str, card_seq: int, ck: bytes) -> bytes:
    """``seal_to(dk_kx, purpose "card", recipient = device_id, object = workspace_id, epoch = card_seq, CK)`` (§5.3,
    §7.1): the ``sealed`` of one ``card_key_wraps`` entry. ``card_seq`` is the card's, ``1 <= card_seq < 2^32`` (the
    seal's epoch field, as ``orch.wsproto.card`` bounds it)."""
    if type(card_seq) is not int or not 1 <= card_seq <= MAX_EPOCH:
        raise _bad("card_seq")
    return _seal_key("card", dk_kx_pub, device_id, workspace_id, card_seq, ck)


def _sealed(value: object) -> bytes:
    """A sealed 32-byte key: ``eph_pub`` (a valid point) followed by exactly 48 bytes (§19.7 ``_sealed_shape``)."""
    if type(value) is not bytes or len(value) != SEALED_KEY_LEN:
        raise _bad("sealed")
    _pub(value[: crypto.PUB_LEN], "sealed")
    return value


def wk_grant_o(workspace_id: str, device_id: str, epoch: int, sealed: bytes) -> dict[str, Any]:
    """The unsigned §7.3 grant ``o`` for :func:`seal_wk`'s output (R6 signs it through ``RelaySigner.wk_grant``)."""
    return {
        "v": 2,
        "suite": crypto.SUITE_ID,
        "kind": "wk_grant",
        "workspace_id": crypto.hex_id(_id(workspace_id, "workspace_id")),
        "device_id": crypto.hex_id(_id(device_id, "device_id")),
        "epoch": _epoch(epoch),
        "sealed": crypto.b64u(_sealed(sealed)),
    }


@dataclass(frozen=True, slots=True)
class Grant:
    """A WK grant that passed :func:`check_wk_grant`: still sealed (only the device's ``dk_kx`` opens it)."""

    workspace_id: str
    device_id: str
    epoch: int
    sealed: bytes


def check_wk_grant(signed: object, wsk_pub: bytes, workspace_id: str, device_id: str) -> Grant:
    """§7.3 as the relay reference's ``open_wk_grant`` orders it, without the unsealing: the ``WSK`` signature
    (``bad_signature``), the workspace and the device (``not_for_this_device``), then the exact shape (``malformed``:
    field set, epoch, the sealed blob's length and ephemeral point)."""
    wsk = _pub(wsk_pub, "wsk_pub")
    ws = crypto.hex_id(_id(workspace_id, "workspace_id"))
    dev = crypto.hex_id(_id(device_id, "device_id"))
    o = _verify_signed(wsk, signed, "wk_grant")
    if o is None:
        raise Refused("bad_signature", "wk_grant")
    if o.get("workspace_id") != ws or o.get("device_id") != dev:
        raise Refused("not_for_this_device", "wk_grant")
    if set(o) != GRANT_FIELDS:
        raise Refused("malformed", "wk_grant field set")
    try:
        epoch = _epoch(o["epoch"])
        sealed = _sealed(crypto.unb64u(o["sealed"]))
    except (Refused, crypto.EncodingError):
        raise Refused("malformed", "wk_grant epoch or sealed") from None
    return Grant(workspace_id=ws, device_id=dev, epoch=epoch, sealed=sealed)
