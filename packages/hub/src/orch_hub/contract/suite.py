"""Suite 2 (protocol-v2 §1): ECDSA P-256/SHA-256 with raw ``r || s``, 65-byte uncompressed public keys.

Same rules as orch-core ``orch.crypto.suite``: a signature is 64 bytes with ``1 <= r, s <= n-1`` (high-s accepted, so
no signature is ever an identifier); a public key is a 65-byte uncompressed point on the curve. Real signers
randomise; :func:`sign` with ``deterministic=True`` (RFC 6979) exists only so the vectors are reproducible.
"""

from __future__ import annotations

import hashlib

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature, encode_dss_signature

SUITE_ID = 2
PUB_LEN = 65
SIG_LEN = 64
P256_N = 0xFFFFFFFF00000000FFFFFFFFFFFFFFFFBCE6FAADA7179E84F3B9CAC2FC632551
_CURVE = ec.SECP256R1()


def sha256(data: bytes) -> bytes:
    return hashlib.sha256(data).digest()


def fake_seed(label: str) -> bytes:
    """FAKE test material, as orch-relay's vectors: SHA-256("FAKE orch v2 test vector: " || label)."""
    return sha256(b"FAKE orch v2 test vector: " + label.encode())


def key_from_seed(seed: bytes) -> ec.EllipticCurvePrivateKey:
    """The relay reference's seed -> scalar map: ``int(seed) mod (n-1) + 1``."""
    return ec.derive_private_key(int.from_bytes(seed, "big") % (P256_N - 1) + 1, _CURVE)


def public_bytes(key: ec.EllipticCurvePrivateKey) -> bytes:
    return key.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)


def valid_public_key(pub: object) -> bool:
    if type(pub) is not bytes or len(pub) != PUB_LEN or pub[0] != 4:
        return False
    try:
        ec.EllipticCurvePublicKey.from_encoded_point(_CURVE, pub)
    except ValueError:
        return False
    return True


def sign(key: ec.EllipticCurvePrivateKey, msg: bytes, *, deterministic: bool = False) -> bytes:
    r, s = decode_dss_signature(key.sign(msg, ec.ECDSA(hashes.SHA256(), deterministic_signing=deterministic)))
    return r.to_bytes(32, "big") + s.to_bytes(32, "big")


def verify(pub: bytes, sig: bytes, msg: bytes) -> bool:
    """True iff ``sig`` is a valid raw signature over ``msg`` under ``pub``; anything malformed is False."""
    if type(sig) is not bytes or len(sig) != SIG_LEN or not valid_public_key(pub):
        return False
    r, s = int.from_bytes(sig[:32], "big"), int.from_bytes(sig[32:], "big")
    if not (0 < r < P256_N and 0 < s < P256_N):
        return False
    try:
        ec.EllipticCurvePublicKey.from_encoded_point(_CURVE, pub).verify(
            encode_dss_signature(r, s), msg, ec.ECDSA(hashes.SHA256())
        )
    except InvalidSignature:
        return False
    return True
