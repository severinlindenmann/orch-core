"""The sealed field (docs/architecture/orch-v2-sealed-field.md, #1198 C5): a secret between the browser and the click
signer that the dashboard HTTP server, a proxy and their logs only ever relay as ciphertext.

One construction, two directions (ECIES-style, single shot, WebCrypto-compatible):

* ``in``  (browser -> signer): typed secrets (``hub.backup_key``, ``device.recovery_code``, ``custody.passphrase``,
  ``github.token``), sealed to the signer's per-request key that the View carries (so ``view_digest`` covers it).
* ``out`` (signer -> browser): ``grant.secret``, sealed to the key the page sent with the sign call; shown once.

P-256 ECDH (the shared secret is the 32-byte x coordinate, WebCrypto ``deriveBits(..., 256)``), HKDF-SHA256 (salt
``orch-sealed-v1``, info = the AAD, 44 bytes: AES key then nonce), AES-256-GCM with the AAD. The AAD binds the
direction, the purpose, the workspace id, the request id, the view digest and both public keys. Every seal uses a
fresh ephemeral key that is used once, so the derived nonce never repeats.

This module is signer-side (it needs ``cryptography``); the dashboard server never imports it and never holds a
private key. It is not wired into the signer here (C7/C10/C13 do that).
"""

from __future__ import annotations

import base64
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from .protocol import REQ, WS_ID

__all__ = [
    "ALG",
    "BLOCK",
    "MAX_SECRET",
    "PURPOSES",
    "REASONS",
    "VERSION",
    "Context",
    "Recipient",
    "SealedError",
    "seal",
]

VERSION = 1
ALG = "P256-HKDF-SHA256-A256GCM"
LABEL = b"orch-sealed-v1"
#: purpose -> direction; a closed set (a new secret is a contract change, with vectors on both sides)
PURPOSES: Mapping[str, str] = {
    "hub.backup_key": "in",
    "device.recovery_code": "in",
    "custody.passphrase": "in",
    "github.token": "in",
    "grant.secret": "out",
}
#: the padded plaintext is a whole number of blocks (hides the length of a passphrase to within 256 bytes)
BLOCK = 256
MAX_PADDED = 4096
MAX_SECRET = MAX_PADDED - 2
TAG = 16
#: refusal reasons (static codes, safe to show and log; never the secret, never the crypto detail)
REASONS = ("malformed", "context", "open_failed", "replay", "closed", "too_large")

_DIGEST = re.compile(r"sha256:[0-9a-f]{64}")
_B64URL = re.compile(r"[A-Za-z0-9_-]+")
_PUB_LEN = 65
_ENVELOPE_KEYS = frozenset({"v", "alg", "epk", "ct"})


class SealedError(Exception):
    """A refused seal or open; ``reason`` is one of :data:`REASONS`."""

    def __init__(self, reason: str, message: str) -> None:
        assert reason in REASONS
        super().__init__(message)
        self.reason = reason


@dataclass(frozen=True)
class Context:
    """What a sealed value is bound to. The opener builds it from its own state, never from the envelope."""

    purpose: str
    workspace_id: str
    request_id: str
    view_digest: str

    def __post_init__(self) -> None:
        if self.purpose not in PURPOSES:
            raise SealedError("context", "unknown purpose")
        if type(self.workspace_id) is not str or not WS_ID.fullmatch(self.workspace_id):
            raise SealedError("context", "workspace_id is a workspace id")
        if type(self.request_id) is not str or not REQ.fullmatch(self.request_id):
            raise SealedError("context", "request_id is a request id")
        if type(self.view_digest) is not str or not _DIGEST.fullmatch(self.view_digest):
            raise SealedError("context", "view_digest is a sha256 hash")

    @property
    def direction(self) -> str:
        return PURPOSES[self.purpose]


def b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _unb64url(text: Any, what: str) -> bytes:
    """Strict, canonical base64url without padding (a second spelling of the same bytes is refused)."""
    if type(text) is not str or not _B64URL.fullmatch(text) or len(text) % 4 == 1:
        raise SealedError("malformed", f"{what} is not base64url")
    raw = base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))
    if b64url(raw) != text:
        raise SealedError("malformed", f"{what} is not canonical base64url")
    return raw


def encode_public(key: ec.EllipticCurvePublicKey) -> str:
    """The uncompressed SEC1 point (WebCrypto ``exportKey("raw")``), base64url."""
    from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

    return b64url(key.public_bytes(Encoding.X962, PublicFormat.UncompressedPoint))


def decode_public(text: Any, what: str = "public key") -> tuple[ec.EllipticCurvePublicKey, bytes]:
    """A P-256 public key (65 bytes, ``0x04||X||Y``, on the curve); returns the key and its raw bytes."""
    raw = _unb64url(text, what)
    if len(raw) != _PUB_LEN or raw[0] != 4:
        raise SealedError("malformed", f"{what} is not an uncompressed P-256 point")
    try:
        return ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), raw), raw
    except ValueError:
        raise SealedError("malformed", f"{what} is not a point on P-256") from None


def aad(ctx: Context, epk: bytes, rpk: bytes) -> bytes:
    """Each field as a 2-byte big-endian length then its bytes, in this fixed order."""
    parts = [
        LABEL,
        ctx.direction.encode(),
        ctx.purpose.encode(),
        ctx.workspace_id.encode(),
        ctx.request_id.encode(),
        ctx.view_digest.encode(),
        epk,
        rpk,
    ]
    return b"".join(len(p).to_bytes(2, "big") + p for p in parts)


def derive(shared: bytes, ad: bytes) -> tuple[bytes, bytes]:
    """HKDF-SHA256(ikm=shared, salt=LABEL, info=AAD, 44 bytes) -> (AES-256 key, 12-byte nonce)."""
    okm = HKDF(algorithm=hashes.SHA256(), length=44, salt=LABEL, info=ad).derive(shared)
    return okm[:32], okm[32:]


def pad(secret: bytes) -> bytes:
    n = len(secret)
    if n == 0:
        raise SealedError("malformed", "the secret is empty")
    if n > MAX_SECRET:
        raise SealedError("too_large", f"the secret is longer than {MAX_SECRET} bytes")
    body = n.to_bytes(2, "big") + secret
    return body + b"\x00" * (-len(body) % BLOCK)


def unpad(padded: bytes) -> bytes:
    if not padded or len(padded) % BLOCK or len(padded) > MAX_PADDED:
        raise SealedError("malformed", "the sealed value has a wrong size")
    n = int.from_bytes(padded[:2], "big")
    if not 1 <= n <= len(padded) - 2 or len(padded) != len(pad(b"\x01" * n)) or any(padded[2 + n :]):
        raise SealedError("malformed", "the sealed value has a wrong padding")
    return padded[2 : 2 + n]


def _parse(envelope: Any) -> tuple[ec.EllipticCurvePublicKey, bytes, bytes]:
    if not isinstance(envelope, Mapping) or set(envelope) != _ENVELOPE_KEYS:
        raise SealedError("malformed", "a sealed value has exactly v, alg, epk and ct")
    if type(envelope["v"]) is not int or envelope["v"] != VERSION or envelope["alg"] != ALG:
        raise SealedError("malformed", "unknown sealed-value version or algorithm")
    epk, epk_raw = decode_public(envelope["epk"], "epk")
    ct = _unb64url(envelope["ct"], "ct")
    body = len(ct) - TAG
    if body <= 0 or body % BLOCK or body > MAX_PADDED:
        raise SealedError("malformed", "the sealed value has a wrong size")
    return epk, epk_raw, ct


def seal(
    recipient_public: str,
    secret: bytes | bytearray | str,
    ctx: Context,
    *,
    ephemeral: ec.EllipticCurvePrivateKey | None = None,
) -> dict[str, Any]:
    """Seal ``secret`` to ``recipient_public`` for ``ctx``. ``ephemeral`` is for test vectors only: in use every
    seal makes a fresh key, uses it once and drops it (the nonce is derived, so a reused key would reuse it)."""
    rpk, rpk_raw = decode_public(recipient_public, "recipient key")
    data = secret.encode("utf-8") if isinstance(secret, str) else bytes(secret)
    padded = pad(data)
    eph = ephemeral if ephemeral is not None else ec.generate_private_key(ec.SECP256R1())
    epk = encode_public(eph.public_key())
    epk_raw = _unb64url(epk, "epk")
    if epk_raw == rpk_raw:
        raise SealedError("malformed", "the ephemeral key is the recipient key")
    ad = aad(ctx, epk_raw, rpk_raw)
    key, nonce = derive(eph.exchange(ec.ECDH(), rpk), ad)
    return {"v": VERSION, "alg": ALG, "epk": epk, "ct": b64url(AESGCM(key).encrypt(nonce, padded, ad))}


class Recipient:
    """An ephemeral P-256 key that opens sealed values of one direction.

    ``in``: the signer makes one per approval when it builds a View that asks for a secret; the View carries
    :attr:`public` (so ``view_digest`` covers it), the private key stays in the signer process (never written, never
    sent) and is dropped with :meth:`close` when the approval ends. ``out``: the browser's side (here for tests and
    the vectors). Every ``epk`` opens once (``replay`` after that).
    """

    def __init__(self, direction: str, *, private_key: ec.EllipticCurvePrivateKey | None = None) -> None:
        if direction not in ("in", "out"):
            raise ValueError("direction is in or out")
        self.direction = direction
        self._key: ec.EllipticCurvePrivateKey | None = private_key or ec.generate_private_key(ec.SECP256R1())
        self.public = encode_public(self._key.public_key())
        self._raw = _unb64url(self.public, "public key")
        self._seen: set[bytes] = set()

    @property
    def closed(self) -> bool:
        return self._key is None

    def open(self, envelope: Any, ctx: Context) -> bytearray:
        """The secret, as a ``bytearray`` the caller wipes after use. Refuses with :class:`SealedError`."""
        if self._key is None:
            raise SealedError("closed", "this sealed field is closed")
        if ctx.direction != self.direction:
            raise SealedError("context", "this purpose is not opened here")
        epk, epk_raw, ct = _parse(envelope)
        if epk_raw == self._raw:
            raise SealedError("malformed", "the ephemeral key is the recipient key")
        if epk_raw in self._seen:
            raise SealedError("replay", "this sealed value was opened already")
        ad = aad(ctx, epk_raw, self._raw)
        key, nonce = derive(self._key.exchange(ec.ECDH(), epk), ad)
        try:
            padded = bytearray(AESGCM(key).decrypt(nonce, ct, ad))
        except InvalidTag:
            raise SealedError("open_failed", "the sealed value does not open for this request") from None
        try:
            out = bytearray(unpad(bytes(padded)))
        finally:
            padded[:] = b"\x00" * len(padded)
        self._seen.add(epk_raw)
        return out

    def close(self) -> None:
        """Drop the private key (best effort in Python: the object is released, nothing is written anywhere)."""
        self._key = None
        self._seen.clear()
