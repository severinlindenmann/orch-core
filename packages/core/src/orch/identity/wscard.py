"""The workspace card for R6 (design 11A.15.10 item 7, review round 1 of #495): the rotation's card ``o`` with its
sealed part and ``ck_commit``, and the **typed** verified card every card consumer takes.

Specs: ``contracts/relay/protocol-v2.md`` §7.1. Vectors: the relay corpus ``card`` section
(``tests/identity/test_wscard.py``).

A :class:`VerifiedCard` exists only through :func:`verify_card`, which runs ``orch.wsproto.card.verify_card`` (§7.1
checks 1 to 6 under the owner's ``PK``). A function that consumes a card takes this type, never a dict, so an
unverified card (say one served by the relay with an attacker's ``wsk_pub``) cannot reach it (Opus M1). The relay's
update rules (``accept_card``: rollback, ``wsk_changed``, ...) stay the caller's, against its stored card.

No I/O, no key held. ``CK`` is a parameter (fresh per ``card_seq``, R6's custody) and never appears in a message.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from orch import canon, crypto
from orch.wsproto import card as relay_card

from . import _mint
from .errors import Refused
from .pairing import MAX_EPOCH, _bad, _id, _pub, _raw

__all__ = ["CARD_PART_FIELDS", "VerifiedCard", "card_o", "verify_card"]

MAX_SAFE_INT = 2**53 - 1
CARD_PART_FIELDS = frozenset({"name", "description", "capabilities", "hosted_by"})


@dataclass(frozen=True, slots=True)
class VerifiedCard:
    """A card that passed §7.1 checks 1 to 6 under the owner's ``PK``. Its ``mint`` binds every field
    (``orch.identity._mint``): constructing one anywhere but :func:`verify_card`, ``dataclasses.replace`` or any
    field change is refused, and consumers re-check it. ``o`` is a fresh copy of the verified card object on every
    read (stored as ``cj`` bytes, read-only)."""

    workspace_id: str
    owner_person_id: str
    wsk_pub: bytes
    card_seq: int
    relay_url: str
    client_hosted: bool
    o_cj: bytes = field(repr=False)
    mint: bytes = field(default=b"", repr=False, compare=False)

    def __post_init__(self) -> None:
        _mint.check(self, VerifiedCard)

    def __init_subclass__(cls, **kw: Any) -> None:
        _mint.no_subclass(cls)

    @property
    def o(self) -> dict[str, Any]:
        return canon.loads_strict(self.o_cj)


def verify_card(card: object, owner_pk_pub: bytes) -> VerifiedCard:
    """``orch.wsproto.card.verify_card`` (its codes: ``malformed``, ``wrong_suite``, ``other_person``,
    ``bad_signature``, ``delegation_mismatch``) as :class:`Refused`, and the typed result. ``owner_pk_pub`` is the
    pinned owner key (the genesis delegation), never one the relay served."""
    pk = _pub(owner_pk_pub, "owner_pk_pub")
    try:
        o = relay_card.verify_card(card, pk)
    except relay_card.CardRefused as e:
        raise Refused(e.code, "card") from None
    assert isinstance(card, dict)  # verify_card checked the shape
    return _mint.make(
        VerifiedCard,
        workspace_id=o["workspace_id"],
        owner_person_id=o["owner_person_id"],
        wsk_pub=crypto.unb64u(o["wsk_pub"], crypto.PUB_LEN),
        card_seq=o["card_seq"],
        relay_url=o["relay_url"],
        client_hosted=card["delegation"]["o"]["client_hosted"],
        o_cj=canon.dumps(o),
    )


def _part(part: object) -> dict[str, Any]:
    """The sealed part: exactly ``{name, description, capabilities, hosted_by}``, strings, a list of strings, and
    ``hosted_by`` a string or ``None`` (§7.1)."""
    if type(part) is not dict or set(part) != CARD_PART_FIELDS:
        raise _bad("card part field set")
    if type(part["name"]) is not str or type(part["description"]) is not str:
        raise _bad("card part name or description")
    caps = part["capabilities"]
    if type(caps) is not list or any(type(c) is not str for c in caps):
        raise _bad("card part capabilities")
    if part["hosted_by"] is not None and type(part["hosted_by"]) is not str:
        raise _bad("card part hosted_by")
    return {"name": part["name"], "description": part["description"], "capabilities": list(caps),
            "hosted_by": part["hosted_by"]}  # fmt: skip


def card_o(
    *,
    workspace_id: str,
    wsk_pub: bytes,
    wxk_pub: bytes,
    wxk_version: int,
    owner_person_id: str,
    relay_url: str,
    card_seq: int,
    issued_ms: int,
    ck: bytes,
    part: dict[str, Any],
) -> tuple[dict[str, Any], bytes]:
    """The unsigned §7.1 card ``o`` and its sealed part, for a new ``card_seq`` (the WXK rotation, 11A.15.5):
    ``sealed = AES-256-GCM(HKDF(CK, "", "orch/v2/card|ws|card_seq"), zero nonce, cj(part), aad)``,
    ``sealed_hash = b64u(H(sealed))`` and ``ck_commit = b64u(H("orch/v2/card-key-commit|" ws u32(card_seq) CK))``.
    ``CK`` must be fresh per ``card_seq`` (the zero nonce relies on it): R6's custody draws it. R6 signs ``o`` through
    ``RelaySigner`` (``sig_card_wsk``); the delegation is the genesis one, unchanged."""
    ws = crypto.hex_id(_id(workspace_id, "workspace_id"))
    owner = crypto.hex_id(_id(owner_person_id, "owner_person_id"))
    wsk, wxk = _pub(wsk_pub, "wsk_pub"), _pub(wxk_pub, "wxk_pub")
    if not relay_card.relay_url_ok(relay_url):
        raise _bad("relay_url")
    if type(wxk_version) is not int or not 1 <= wxk_version <= MAX_SAFE_INT:
        raise _bad("wxk_version")
    if type(card_seq) is not int or not 1 <= card_seq <= MAX_EPOCH:  # u32 in the AAD
        raise _bad("card_seq")
    if type(issued_ms) is not int or not 0 <= issued_ms <= MAX_SAFE_INT:
        raise _bad("issued_ms")
    key = _raw(ck, 32, "ck")
    try:
        sealed = relay_card.seal_card_part(key, ws, card_seq, _part(part))
    except (relay_card.CardRefused, canon.JcsError):
        raise _bad("card part") from None
    o = {
        "v": 2,
        "suite": crypto.SUITE_ID,
        "kind": "card",
        "workspace_id": ws,
        "wsk_pub": crypto.b64u(wsk),
        "wxk_pub": crypto.b64u(wxk),
        "wxk_version": wxk_version,
        "owner_person_id": owner,
        "relay_url": relay_url,
        "card_seq": card_seq,
        "issued_ms": issued_ms,
        "sealed_hash": relay_card.sealed_hash(sealed),
        "ck_commit": relay_card.ck_commit(ws, card_seq, key),
    }
    return o, sealed
