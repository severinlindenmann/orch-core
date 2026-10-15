"""Pure builders for R6 pairing (design 11A.15.10, H12c items 1-4): the epoch-key commitment, the two 6-character codes,
the pairing links and the pair request's meta.

Specs: orch-relay ``docs/protocol-v2.md`` §2.5 (labels), §4 (pins, SAS, ``cert_code``), §7.2 (``wk_commit``), §8.3
(links, the pair meta), §8.4 (the challenge). Vectors: the relay corpus sections ``wk_commit``, ``bridge.sas``,
``cert_request.cert_code``, ``bridge.links`` and the epoch-0 ``bridge.host_cases`` (``tests/identity/test_pairing.py``).

No I/O, no clock, no key held. Every function validates its inputs exactly and raises :class:`Refused` with code
``malformed`` (the protocol's code for a bad meta shape) on anything outside the contract; the message never carries a
secret.
"""

from __future__ import annotations

import base64

from orch import canon, crypto
from orch.crypto.labels import L

from .errors import Refused

__all__ = ["CHALLENGE_FIELDS", "MAX_EPOCH", "cert_code", "sas", "wk_commit"]

MAX_EPOCH = 2**32 - 1  # protocol §7.2: an epoch is a u32 in 1 .. 2^32-1 wherever it is parsed


def _bad(what: str) -> Refused:
    return Refused("malformed", what)


def _id(value: object, what: str) -> bytes:
    """A 16-byte id in its one text form, 32 lower-case hex characters (protocol §2.2)."""
    try:
        return crypto.unhex(value, 16)
    except crypto.EncodingError:
        raise _bad(what) from None


def _raw(value: object, n: int, what: str) -> bytes:
    if type(value) is not bytes or len(value) != n:
        raise _bad(what)
    return value


def _pub(value: object, what: str) -> bytes:
    """A suite-2 public key: 65 raw bytes, uncompressed, on the curve (``dk_sig``, ``dk_kx``, ``WSK``, ``PK``)."""
    if type(value) is not bytes:
        raise _bad(what)
    try:
        return crypto.validate_public_key(value)
    except crypto.CryptoError:
        raise _bad(what) from None


def _b64_pub(value: object, what: str) -> bytes:
    try:
        raw = crypto.unb64u(value, crypto.PUB_LEN)
    except crypto.EncodingError:
        raise _bad(what) from None
    return _pub(raw, what)


def _lp16(b: bytes) -> bytes:
    return len(b).to_bytes(2, "big") + b


def _code(digest: bytes) -> str:
    """The first 6 characters of RFC 4648 base32 (``A-Z2-7``, 30 bits) of a 32-byte digest."""
    return base64.b32encode(digest).decode("ascii")[:6]


# --- 1. wk_commit (§7.2, G2b) ----------------------------------------------------------------------------------------


def wk_commit(workspace_id: str, epoch: int, wk: bytes) -> str:
    """``b64u(H("orch/v2/wk-commit|" || workspace_id || u32(epoch) || WK_epoch))``: the key commitment every member list
    at ``epoch`` carries. No suite byte (unlike the pins), as the protocol and the vectors have it."""
    ws = _id(workspace_id, "workspace_id")
    if type(epoch) is not int or not 1 <= epoch <= MAX_EPOCH:
        raise _bad("epoch")
    key = _raw(wk, 32, "wk")
    return crypto.b64u(crypto.sha256(L["h_wk_commit"] + ws + epoch.to_bytes(4, "big") + key))


# --- 2. the two codes (§4, §8.4) -------------------------------------------------------------------------------------


def sas(workspace_id: str, offer_id: str, dk_sig_pub: bytes, dk_kx_pub: bytes, wsk_pub: bytes, sas_nonce: bytes) -> str:
    """The 6-character pairing code: ``base32(H("orch/v2/sas|" || suite || ws || offer || lp16(dk_sig_pub) ||
    lp16(dk_kx_pub) || lp16(wsk_pub) || sas_nonce))[0:6]``. Ids are 32 lower-case hex; keys are raw 65-byte points (the
    wire's b64u decoded); ``sas_nonce`` is the offer's 32 raw bytes. The host shows it for the request it consumed;
    the device for the signed ``pending`` answer."""
    ws, offer = _id(workspace_id, "workspace_id"), _id(offer_id, "offer_id")
    keys = [_pub(k, w) for k, w in ((dk_sig_pub, "dk_sig_pub"), (dk_kx_pub, "dk_kx_pub"), (wsk_pub, "wsk_pub"))]
    nonce = _raw(sas_nonce, 32, "sas_nonce")
    pre = L["h_sas"] + bytes([crypto.SUITE_ID]) + ws + offer + b"".join(_lp16(k) for k in keys) + nonce
    return _code(crypto.sha256(pre))


CHALLENGE_FIELDS = frozenset(
    {
        "v",
        "suite",
        "kind",
        "request_id",
        "workspace_id",
        "person_id",
        "offer_id",
        "attempt",
        "device_id",
        "dk_sig_pub",
        "dk_kx_pub",
        "primary_nonce",
        "expires_ms",
    }
)
_CHALLENGE_IDS = ("request_id", "workspace_id", "person_id", "offer_id", "attempt", "device_id")
_MAX_SAFE_INT = 2**53 - 1


def cert_code(challenge_o: dict) -> str:
    """The 6-character code of the primary's challenge: ``base32(H("orch/v2/sas-cert|" || suite || cj(o)))[0:6]``, over
    the challenge **object** ``o`` (not the ``{o, sig}`` wrapper). The object must have the exact §8.4 (G3b) field set
    and types: an old-shape challenge (no ``offer_id``/``attempt``) has no code. This is the code only; the
    signature, the pins and the binding to the attempt are the caller's checks (§8.4 step 5)."""
    o = challenge_o
    if type(o) is not dict or set(o) != CHALLENGE_FIELDS:
        raise _bad("challenge field set")
    if o["v"] != 2 or type(o["v"]) is not int or o["suite"] != crypto.SUITE_ID or type(o["suite"]) is not int:
        raise _bad("challenge version or suite")
    if o["kind"] != "cert_challenge":
        raise _bad("challenge kind")
    for k in _CHALLENGE_IDS:
        _id(o[k], k)
    _b64_pub(o["dk_sig_pub"], "dk_sig_pub")
    _b64_pub(o["dk_kx_pub"], "dk_kx_pub")
    try:
        crypto.unb64u(o["primary_nonce"], 32)
    except crypto.EncodingError:
        raise _bad("primary_nonce") from None
    if type(o["expires_ms"]) is not int or not 0 <= o["expires_ms"] <= _MAX_SAFE_INT:
        raise _bad("expires_ms")
    return _code(crypto.sha256(L["h_sas_cert"] + bytes([crypto.SUITE_ID]) + canon.dumps(o)))
