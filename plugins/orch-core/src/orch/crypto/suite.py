"""Suite 2 primitives (protocol v2 §1, D46): ECDSA P-256/SHA-256 with raw ``r || s``, ECDH P-256 (x-coordinate),
HKDF-SHA-256, AES-256-GCM, SALTED-AEAD. Everything over ``cryptography``; nothing here reads a key from disk.

Decisions:

* **Signature.** A signature is exactly 64 bytes with ``1 <= r, s <= n-1`` (§1.2). High-s is accepted (the twin of a
  valid signature verifies; no signature is ever an identifier). Signing is randomised (not RFC 6979).
* **Public keys** are 65-byte uncompressed points on the curve. The identity, compressed, hybrid and wrong-length
  encodings are refused before any use (:func:`validate_public_key`).
* **Private keys** are ``cryptography`` objects; this module never exports a scalar except through
  :func:`private_scalar` (custody and the recovery code need it).
* **ECDH** returns the 32-byte x-coordinate and refuses an all-zero result.
"""

from __future__ import annotations

import secrets
from typing import TYPE_CHECKING

from cryptography.exceptions import InvalidSignature, InvalidTag
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature, encode_dss_signature
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from .labels import L

if TYPE_CHECKING:
    from cryptography.hazmat.primitives.asymmetric.ec import EllipticCurvePrivateKey

__all__ = [
    "P256_N",
    "PUB_LEN",
    "SIG_LEN",
    "SUITE_ID",
    "CryptoError",
    "InvalidTag",
    "aes_gcm_open",
    "aes_gcm_seal",
    "device_id",
    "ecdh",
    "generate_private_key",
    "hkdf",
    "person_id",
    "pk_pin",
    "private_key_from_scalar",
    "private_scalar",
    "public_bytes",
    "salted_open",
    "salted_seal",
    "sha256",
    "sign",
    "validate_public_key",
    "verify",
    "wsk_pin",
]

SUITE_ID = 2
PUB_LEN = 65
SIG_LEN = 64
P256_N = 0xFFFFFFFF00000000FFFFFFFFFFFFFFFFBCE6FAADA7179E84F3B9CAC2FC632551
ZERO_NONCE = bytes(12)

_CURVE = ec.SECP256R1()


class CryptoError(ValueError):
    """An input to a primitive is not valid for suite 2 (key, signature, length)."""


def sha256(data: bytes) -> bytes:
    h = hashes.Hash(hashes.SHA256())
    h.update(data)
    return h.finalize()


# --- keys -------------------------------------------------------------------------------------------------------


def generate_private_key() -> EllipticCurvePrivateKey:
    return ec.generate_private_key(_CURVE)


def private_key_from_scalar(d: int) -> EllipticCurvePrivateKey:
    if type(d) is not int or not 1 <= d <= P256_N - 1:
        raise CryptoError("private scalar out of range")
    return ec.derive_private_key(d, _CURVE)


def private_scalar(key: EllipticCurvePrivateKey) -> bytes:
    """The 32-byte big-endian private scalar (custody and recovery-code derivation only)."""
    return key.private_numbers().private_value.to_bytes(32, "big")


def public_bytes(key: EllipticCurvePrivateKey) -> bytes:
    """The 65-byte uncompressed public point of a private key."""
    return key.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)


def _load_public(pub: object) -> ec.EllipticCurvePublicKey:
    if type(pub) is not bytes or len(pub) != PUB_LEN or pub[0] != 4:
        raise CryptoError("not a 65-byte uncompressed P-256 point")
    try:
        return ec.EllipticCurvePublicKey.from_encoded_point(_CURVE, pub)  # on-curve check; refuses the identity
    except ValueError as e:
        raise CryptoError("point is not on the P-256 curve") from e


def validate_public_key(pub: object) -> bytes:
    """Return ``pub`` if it is a valid suite-2 public key (65-byte uncompressed, on the curve), else raise."""
    _load_public(pub)
    return pub  # type: ignore[return-value]


# --- signatures ---------------------------------------------------------------------------------------------------


def sign(key: EllipticCurvePrivateKey, msg: bytes) -> bytes:
    """ECDSA P-256/SHA-256 over ``msg``, raw 64-byte ``r || s``."""
    r, s = decode_dss_signature(key.sign(msg, ec.ECDSA(hashes.SHA256())))
    return r.to_bytes(32, "big") + s.to_bytes(32, "big")


def verify(pub: bytes, sig: bytes, msg: bytes) -> bool:
    """True iff ``sig`` is a 64-byte raw signature with ``1 <= r, s <= n-1`` that verifies ``msg`` under ``pub``.
    High-s is accepted. Any malformed input is simply False."""
    if type(sig) is not bytes or len(sig) != SIG_LEN or type(msg) is not bytes:
        return False
    r, s = int.from_bytes(sig[:32], "big"), int.from_bytes(sig[32:], "big")
    if not (0 < r < P256_N and 0 < s < P256_N):
        return False
    try:
        _load_public(pub).verify(encode_dss_signature(r, s), msg, ec.ECDSA(hashes.SHA256()))
    except (InvalidSignature, CryptoError, ValueError):
        return False
    return True


# --- agreement, KDF, AEAD -------------------------------------------------------------------------------------------


def ecdh(priv: EllipticCurvePrivateKey, pub: bytes) -> bytes:
    """The 32-byte x-coordinate shared secret; refuses an invalid peer key and an all-zero result."""
    ss = priv.exchange(ec.ECDH(), _load_public(pub))
    if not any(ss):
        raise CryptoError("all-zero shared secret")
    return ss


def hkdf(ikm: bytes, salt: bytes, info: bytes, length: int = 32) -> bytes:
    """RFC 5869 HKDF-SHA-256; an empty salt is 32 zero bytes (as WebCrypto does)."""
    return HKDF(hashes.SHA256(), length, salt or None, info).derive(ikm)


def aes_gcm_seal(key: bytes, nonce: bytes, pt: bytes, aad: bytes) -> bytes:
    return AESGCM(key).encrypt(nonce, pt, aad)


def aes_gcm_open(key: bytes, nonce: bytes, ct: bytes, aad: bytes) -> bytes:
    return AESGCM(key).decrypt(nonce, ct, aad)


def salted_seal(k_base: bytes, msg_label: bytes, aad: bytes, pt: bytes) -> bytes:
    """SALTED-AEAD (§5.1): ``salt || AES-256-GCM(HKDF(k_base, salt, msg_label), 12 zero bytes, pt, aad)`` with a fresh
    16-byte salt drawn here."""
    return _salted_seal(k_base, msg_label, aad, pt, secrets.token_bytes(16))


def _salted_seal(k_base: bytes, msg_label: bytes, aad: bytes, pt: bytes, salt: bytes) -> bytes:
    """Test seam: the vectors fix the salt. Never call outside tests."""
    if len(salt) != 16:
        raise CryptoError("salt must be 16 bytes")
    return salt + AESGCM(hkdf(k_base, salt, msg_label)).encrypt(ZERO_NONCE, pt, aad)


def salted_open(k_base: bytes, msg_label: bytes, aad: bytes, blob: bytes) -> bytes:
    if len(blob) < 32:
        raise InvalidTag()
    return AESGCM(hkdf(k_base, blob[:16], msg_label)).decrypt(ZERO_NONCE, blob[16:], aad)


# --- ids and pins (protocol §4) -------------------------------------------------------------------------------------


def _suite_hash(label: str, pub: bytes) -> bytes:
    return sha256(L[label] + bytes([SUITE_ID]) + pub)


def person_id(pk_pub: bytes) -> bytes:
    """``H("orch/v2/id/person|" || suite || pk_pub)[0:16]`` (16 raw bytes; text form is 32 hex, ``p_`` + hex in F1)."""
    return _suite_hash("h_person_id", validate_public_key(pk_pub))[:16]


def device_id(dk_sig_pub: bytes) -> bytes:
    """``H("orch/v2/id/device|" || suite || dk_sig_pub)[0:16]``."""
    return _suite_hash("h_device_id", validate_public_key(dk_sig_pub))[:16]


def pk_pin(pk_pub: bytes) -> bytes:
    return _suite_hash("h_pin_person", validate_public_key(pk_pub))


def wsk_pin(wsk_pub: bytes) -> bytes:
    return _suite_hash("h_pin_workspace", validate_public_key(wsk_pub))

