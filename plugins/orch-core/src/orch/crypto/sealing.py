"""Sealing to a device or a workspace exchange key (protocol §5.3), HPKE-shaped:

``sealed = eph_pub || AES-256-GCM(K, 12 zero bytes, pt, AAD)`` with ``K = HKDF(ss, eph_pub || rcpt_pub,
"orch/v2/seal|" purpose "|" hex(recipient_id))``.

The ephemeral key is generated inside :func:`seal` and is never a parameter: reusing it would reuse the AEAD key under
a fixed zero nonce. The deterministic seam for the vectors is :func:`_seal_with_ephemeral` and is test-only.
"""

from __future__ import annotations

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.asymmetric.ec import EllipticCurvePrivateKey

from . import suite
from .labels import LABELS, L

__all__ = ["SEAL_PURPOSES", "open_sealed", "seal", "seal_aad"]

SEAL_PURPOSES = ("wk", "sk", "card", "cert-request", "drop-dek", "ws-envelope", "vault")


def seal_aad(purpose: str, object_id: bytes, epoch: int, extra: bytes = b"") -> bytes:
    p = purpose.encode("ascii")
    if not 0 <= epoch < 2**32:
        raise suite.CryptoError("epoch out of range")
    return L["aad_seal"] + bytes([suite.SUITE_ID, len(p)]) + p + object_id + epoch.to_bytes(4, "big") + extra


def _key(ss: bytes, eph_pub: bytes, rcpt_pub: bytes, purpose: str, rcpt_id: bytes) -> bytes:
    info = f"{LABELS['kdf_seal']}|{purpose}|{rcpt_id.hex()}".encode("ascii")
    return suite.hkdf(ss, eph_pub + rcpt_pub, info)


def _check(purpose: str, object_id: bytes, rcpt_id: bytes) -> None:
    if purpose not in SEAL_PURPOSES:
        raise suite.CryptoError(f"unknown seal purpose {purpose!r}")
    if len(object_id) != 16 or len(rcpt_id) != 16:
        raise suite.CryptoError("object and recipient ids are 16 bytes")


def _seal_with_ephemeral(
    eph: EllipticCurvePrivateKey,
    rcpt_kx_pub: bytes,
    rcpt_id: bytes,
    purpose: str,
    object_id: bytes,
    epoch: int,
    pt: bytes,
    extra_aad: bytes = b"",
) -> bytes:
    """Test seam for the vectors (fixed ephemeral). Never call outside tests."""
    _check(purpose, object_id, rcpt_id)
    eph_pub = suite.public_bytes(eph)
    k = _key(suite.ecdh(eph, rcpt_kx_pub), eph_pub, rcpt_kx_pub, purpose, rcpt_id)
    return eph_pub + suite.aes_gcm_seal(k, suite.ZERO_NONCE, pt, seal_aad(purpose, object_id, epoch, extra_aad))


def seal(
    rcpt_kx_pub: bytes,
    rcpt_id: bytes,
    purpose: str,
    object_id: bytes,
    epoch: int,
    pt: bytes,
    extra_aad: bytes = b"",
) -> bytes:
    """Seal ``pt`` to ``rcpt_kx_pub`` with a fresh ephemeral key pair drawn here."""
    return _seal_with_ephemeral(
        suite.generate_private_key(), rcpt_kx_pub, rcpt_id, purpose, object_id, epoch, pt, extra_aad
    )


def open_sealed(
    rcpt_kx_priv: EllipticCurvePrivateKey,
    rcpt_id: bytes,
    purpose: str,
    object_id: bytes,
    epoch: int,
    blob: bytes,
    extra_aad: bytes = b"",
) -> bytes:
    """Open a sealed blob; any failure (wrong purpose, recipient, object, epoch, key, tampering, bad ephemeral key)
    raises :class:`cryptography.exceptions.InvalidTag`."""
    _check(purpose, object_id, rcpt_id)
    if len(blob) < suite.PUB_LEN + 16:
        raise InvalidTag()
    eph_pub, ct = blob[: suite.PUB_LEN], blob[suite.PUB_LEN :]
    try:
        ss = suite.ecdh(rcpt_kx_priv, eph_pub)
    except suite.CryptoError:
        raise InvalidTag() from None
    k = _key(ss, eph_pub, suite.public_bytes(rcpt_kx_priv), purpose, rcpt_id)
    return suite.aes_gcm_open(k, suite.ZERO_NONCE, ct, seal_aad(purpose, object_id, epoch, extra_aad))
