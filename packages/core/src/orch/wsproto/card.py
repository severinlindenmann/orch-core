"""The workspace card (proto §7.1): the checks a reader makes, in the relay's order and with its codes, the relay's
update rules (so the host can predict ``stale_card`` and friends before it PUTs), and the sealed part.

Pure functions, no I/O. ``CK`` (the card key) is a parameter, never stored here; choosing a fresh ``CK`` per
``card_seq`` and wrapping it to devices is R6's (WXK and card-key custody).
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping
from typing import Any

from cryptography.exceptions import InvalidTag

from orch import canon, crypto
from orch.crypto.labels import L
from orch.wsproto.sign import CARD_FIELDS

__all__ = [
    "CardRefused",
    "accept_card",
    "card_aad",
    "card_key",
    "ck_commit",
    "open_card_part",
    "relay_url_ok",
    "seal_card_part",
    "sealed_hash",
    "signed_bytes",
    "verify_card",
]

MAX_SAFE_INT = 2**53 - 1
# The relay's relay_url form (proto §7.1): https with a lower-case host and optional port; http only for loopback.
_RELAY_URL = re.compile(
    r"https://[a-z0-9]([a-z0-9.-]*[a-z0-9])?(:[1-9][0-9]{0,4})?|http://(localhost|127\.0\.0\.1|\[::1\]|10\.(?:25[0-5]|2[0-4][0-9]|1[0-9]{2}|[1-9]?[0-9])\.(?:25[0-5]|2[0-4][0-9]|1[0-9]{2}|[1-9]?[0-9])\.(?:25[0-5]|2[0-4][0-9]|1[0-9]{2}|[1-9]?[0-9])|172\.(?:1[6-9]|2[0-9]|3[01])\.(?:25[0-5]|2[0-4][0-9]|1[0-9]{2}|[1-9]?[0-9])\.(?:25[0-5]|2[0-4][0-9]|1[0-9]{2}|[1-9]?[0-9])|192\.168\.(?:25[0-5]|2[0-4][0-9]|1[0-9]{2}|[1-9]?[0-9])\.(?:25[0-5]|2[0-4][0-9]|1[0-9]{2}|[1-9]?[0-9])|100\.(?:6[4-9]|[7-9][0-9]|1[01][0-9]|12[0-7])\.(?:25[0-5]|2[0-4][0-9]|1[0-9]{2}|[1-9]?[0-9])\.(?:25[0-5]|2[0-4][0-9]|1[0-9]{2}|[1-9]?[0-9]))(:[1-9][0-9]{0,4})?"
)
_DELEGATION_FIELDS = frozenset(
    {"v", "suite", "kind", "workspace_id", "wsk_pub", "owner_person_id", "client_hosted", "issued_ms"}
)
_ZERO_NONCE = bytes(12)


class CardRefused(ValueError):
    """A card refused with a proto §7.1 code (``malformed``, ``wrong_suite``, ``other_person``, ``bad_signature``,
    ``delegation_mismatch``, ``other_workspace``, ``wsk_changed``, ``delegation_changed``, ``stale_card``,
    ``wxk_changed_without_version``, ``bad_wxk_version``)."""

    def __init__(self, code: str) -> None:
        super().__init__(f"card refused: {code}")
        self.code = code


def relay_url_ok(url: object) -> bool:
    """The card's ``relay_url`` form (proto §7.1): https with a lower-case host and an optional port, no path; http
    only for loopback."""
    return type(url) is str and _RELAY_URL.fullmatch(url) is not None


def signed_bytes(o: Mapping[str, Any]) -> bytes:
    return L["sig_card_wsk"] + canon.dumps(dict(o))


def _delegation(signed: object, pk_pub: bytes) -> dict[str, Any]:
    if type(signed) is not dict or set(signed) != {"o", "sig"}:
        raise CardRefused("bad_signature")
    o = signed["o"]
    if type(o) is not dict or o.get("v") != 2 or o.get("suite") != crypto.SUITE_ID or o.get("kind") != "ws_delegation":
        raise CardRefused("bad_signature")
    try:
        sig = crypto.unb64u(signed["sig"], crypto.SIG_LEN)
        msg = L["sig_ws_delegation"] + canon.dumps(o)
    except (crypto.EncodingError, canon.JcsError):
        raise CardRefused("bad_signature") from None
    if not crypto.verify(pk_pub, sig, msg):
        raise CardRefused("bad_signature")
    if set(o) != _DELEGATION_FIELDS or type(o["client_hosted"]) is not bool or type(o["issued_ms"]) is not int:
        raise CardRefused("malformed")
    if o["owner_person_id"] != crypto.person_id(pk_pub).hex():
        raise CardRefused("other_person")
    return o


def verify_card(c: object, owner_pk_pub: bytes) -> dict[str, Any]:
    """The card's ``o`` if it passes proto §7.1 checks 1 to 6 under the owner's person key, else refused."""
    try:
        if type(c) is not dict or set(c) != {"delegation", "o", "sig", "sealed"}:
            raise CardRefused("malformed")
        o = c["o"]
        if type(o) is not dict or set(o) != CARD_FIELDS or o["v"] != 2 or o["kind"] != "card":
            raise CardRefused("malformed")
        if o["suite"] != crypto.SUITE_ID:
            raise CardRefused("wrong_suite")
        crypto.unhex(o["workspace_id"], 16)
        wsk = crypto.validate_public_key(crypto.unb64u(o["wsk_pub"], crypto.PUB_LEN))
        crypto.validate_public_key(crypto.unb64u(o["wxk_pub"], crypto.PUB_LEN))
        if not relay_url_ok(o["relay_url"]):
            raise CardRefused("malformed")
        for key in ("wxk_version", "card_seq", "issued_ms"):
            low = 0 if key == "issued_ms" else 1
            if type(o[key]) is not int or not low <= o[key] <= MAX_SAFE_INT:
                raise CardRefused("malformed")
        crypto.unb64u(o["ck_commit"], 32)
        sealed = crypto.unb64u(c["sealed"])
        if o["owner_person_id"] != crypto.person_id(owner_pk_pub).hex():
            raise CardRefused("other_person")
        d = _delegation(c["delegation"], owner_pk_pub)
        if any(d[k] != o[k] for k in ("workspace_id", "wsk_pub", "owner_person_id")):
            raise CardRefused("delegation_mismatch")
        # The relay's last step, in its order: the signature first (bad_signature), and only then sealed_hash, whose
        # decoding error is malformed (orch-relay proto verify_card).
        if not crypto.verify(wsk, crypto.unb64u(c["sig"], crypto.SIG_LEN), signed_bytes(o)):
            raise CardRefused("bad_signature")
        if crypto.unb64u(o["sealed_hash"], 32) != crypto.sha256(sealed):
            raise CardRefused("bad_signature")
        return o
    except (crypto.EncodingError, crypto.CryptoError, canon.JcsError, KeyError, TypeError):
        raise CardRefused("malformed") from None


def accept_card(stored: Mapping[str, Any] | None, c: object, owner_pk_pub: bytes) -> dict[str, Any]:
    """:func:`verify_card`, then the relay's update rules against ``stored = {"o": …, "delegation": <its o>}``."""
    o = verify_card(c, owner_pk_pub)
    if stored is None:
        return o
    old = stored["o"]
    assert isinstance(c, dict)
    if any(o[k] != old[k] for k in ("workspace_id", "owner_person_id")):
        raise CardRefused("other_workspace")
    if o["wsk_pub"] != old["wsk_pub"]:
        raise CardRefused("wsk_changed")
    if canon.dumps(c["delegation"]["o"]) != canon.dumps(stored["delegation"]):
        raise CardRefused("delegation_changed")
    if o == old:
        return o
    if o["card_seq"] <= old["card_seq"]:
        raise CardRefused("stale_card")
    if o["wxk_version"] == old["wxk_version"]:
        if o["wxk_pub"] != old["wxk_pub"]:
            raise CardRefused("wxk_changed_without_version")
    elif o["wxk_version"] != old["wxk_version"] + 1 or o["wxk_pub"] == old["wxk_pub"]:
        raise CardRefused("bad_wxk_version")
    return o


# --- the sealed part ------------------------------------------------------------------------------------------------


def _seq(card_seq: int) -> bytes:
    if type(card_seq) is not int or not 1 <= card_seq < 2**32:
        raise CardRefused("malformed")
    return card_seq.to_bytes(4, "big")


def card_key(ck: bytes, workspace_id: str, card_seq: int) -> bytes:
    """``HKDF(CK, "", "orch/v2/card|" ws "|" card_seq)``."""
    _seq(card_seq)
    return crypto.hkdf(ck, b"", f"orch/v2/card|{workspace_id}|{card_seq}".encode("ascii"))


def card_aad(workspace_id: str, card_seq: int) -> bytes:
    return L["aad_card"] + bytes([crypto.SUITE_ID]) + crypto.unhex(workspace_id, 16) + _seq(card_seq)


def seal_card_part(ck: bytes, workspace_id: str, card_seq: int, part: Mapping[str, Any]) -> bytes:
    """``AES-256-GCM(card_key, zero nonce, cj(part), card_aad)``; ``CK`` must be fresh per ``card_seq``."""
    k = card_key(ck, workspace_id, card_seq)
    return crypto.aes_gcm_seal(k, _ZERO_NONCE, canon.dumps(dict(part)), card_aad(workspace_id, card_seq))


def open_card_part(ck: bytes, workspace_id: str, card_seq: int, sealed: bytes) -> dict[str, Any]:
    try:
        pt = crypto.aes_gcm_open(
            card_key(ck, workspace_id, card_seq), _ZERO_NONCE, sealed, card_aad(workspace_id, card_seq)
        )
        return canon.loads_strict(pt)
    except (InvalidTag, ValueError):
        raise CardRefused("bad_signature") from None


def sealed_hash(sealed: bytes) -> str:
    return crypto.b64u(hashlib.sha256(sealed).digest())


def ck_commit(workspace_id: str, card_seq: int, ck: bytes) -> str:
    """``b64u(H("orch/v2/card-key-commit|" || workspace_id || u32(card_seq) || CK))`` (proto §7.1)."""
    return crypto.b64u(crypto.sha256(L["h_card_key_commit"] + crypto.unhex(workspace_id, 16) + _seq(card_seq) + ck))
