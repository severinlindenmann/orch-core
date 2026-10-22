"""The workspace's v2 directory card without a relay (hub contract 9.5, protocol-v2 7.1): the same object a relay
directory holds, built with the relay path's own builder (:func:`orch.identity.wscard.card_o`) and signed by ``WSK``
under ``sig_card_wsk``. The genesis delegation is the owner's ``PK`` signature, unchanged. Pure: the caller supplies
the keys (``WXK``, ``CK``) and keeps them; the card is verified under the owner's ``PK`` before it is returned."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from orch import crypto
from orch.identity import wscard
from orch.identity.errors import Refused
from orch.relay import card as relay_card

from .errors import HubRefused

__all__ = ["NO_RELAY_URL", "build_card"]

#: a card must name a relay in the 7.1 form; a workspace with none names this one (``.invalid`` never resolves)
NO_RELAY_URL = "https://no-relay.invalid"


def build_card(
    *,
    delegation: Mapping[str, Any],
    owner_pk_pub: bytes,
    workspace_id: str,
    wsk_pub: bytes,
    sign_wsk: Callable[[bytes], bytes],
    wxk_pub: bytes,
    wxk_version: int,
    ck: bytes,
    card_seq: int,
    issued_ms: int,
    part: Mapping[str, Any],
    relay_url: str = NO_RELAY_URL,
) -> dict[str, Any]:
    try:
        o, sealed = wscard.card_o(
            workspace_id=workspace_id, wsk_pub=wsk_pub, wxk_pub=wxk_pub, wxk_version=wxk_version,
            owner_person_id=crypto.person_id(owner_pk_pub).hex(), relay_url=relay_url, card_seq=card_seq,
            issued_ms=issued_ms, ck=ck, part=dict(part),
        )  # fmt: skip
        sig = crypto.b64u(sign_wsk(relay_card.signed_bytes(o)))
        card = {"delegation": dict(delegation), "o": o, "sig": sig, "sealed": crypto.b64u(sealed)}
        vc = wscard.verify_card(dict(card), owner_pk_pub)
    except Refused:
        raise HubRefused("malformed") from None
    if vc.workspace_id != workspace_id or vc.wsk_pub != wsk_pub:
        raise HubRefused("delegation_mismatch")
    return card
