"""ECDSA P-256 with SHA-256 (spec §3.4): raw r || s on the wire (WebCrypto's format, never DER), domain-separated,
each scalar checked to lie in 1 .. n-1 before verifying. A signature is malleable ((r, n-s) verifies too), so it is
never used as an identifier."""
from __future__ import annotations

from typing import Callable

from orch.remote.bridge_host import CRYPTO_HINT, MissingCryptography

try:
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature, encode_dss_signature
except ImportError as e:  # pragma: no cover
    raise MissingCryptography(CRYPTO_HINT) from e

L_SIG = b"sharing/bridge/sig/v1|"
P256_N = 0xFFFFFFFF00000000FFFFFFFFFFFFFFFFBCE6FAADA7179E84F3B9CAC2FC632551
SIG_LEN = 64


def signed_bytes(header: bytes, body: bytes) -> bytes:
    """What an envelope signature covers: the label, the header and ciphertext || tag (encrypt-then-sign)."""
    return L_SIG + header + body


def load_public(pub: bytes) -> ec.EllipticCurvePublicKey:
    """A 65-byte uncompressed P-256 point; `from_encoded_point` refuses a point off the curve."""
    if not isinstance(pub, bytes) or len(pub) != 65 or pub[0] != 4:
        raise ValueError("not an uncompressed P-256 point")
    return ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), pub)


def public_bytes(key) -> bytes:
    pub = key.public_key() if isinstance(key, ec.EllipticCurvePrivateKey) else key
    return pub.public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)


def scalars_in_range(sig: bytes) -> bool:
    """64 bytes and both r and s in 1 .. n-1: a rule of its own, not left to the library."""
    if not isinstance(sig, bytes) or len(sig) != SIG_LEN:
        return False
    r, s = int.from_bytes(sig[:32], "big"), int.from_bytes(sig[32:], "big")
    return 0 < r < P256_N and 0 < s < P256_N


def verify(pub: bytes, sig: bytes, msg: bytes) -> bool:
    """A raw r || s signature over `msg`; any problem (length, range, key, signature) is False."""
    if not scalars_in_range(sig):
        return False
    try:
        load_public(pub).verify(encode_dss_signature(int.from_bytes(sig[:32], "big"), int.from_bytes(sig[32:], "big")),
                                msg, ec.ECDSA(hashes.SHA256()))
    except (InvalidSignature, ValueError, TypeError):
        return False
    return True


def verify_der(pub: bytes, der: bytes, msg: bytes) -> bool:
    """A DER signature (WebAuthn assertions, §9.5 step 9 only); any problem is False."""
    try:
        r, s = decode_dss_signature(der)
        if not (0 < r < P256_N and 0 < s < P256_N):
            return False
        load_public(pub).verify(der, msg, ec.ECDSA(hashes.SHA256()))
    except (InvalidSignature, ValueError, TypeError):
        return False
    return True


def sign(key: ec.EllipticCurvePrivateKey, msg: bytes) -> bytes:
    """Raw r || s. Signing may be randomised (this is) or deterministic: verifiers accept either."""
    r, s = decode_dss_signature(key.sign(msg, ec.ECDSA(hashes.SHA256())))
    return r.to_bytes(32, "big") + s.to_bytes(32, "big")


def private_key(d: bytes) -> ec.EllipticCurvePrivateKey:
    return ec.derive_private_key(int.from_bytes(d, "big"), ec.SECP256R1())


def generate(rand: Callable[[int], bytes]) -> ec.EllipticCurvePrivateKey:
    """A new P-256 key from the injected CSPRNG: 32 random bytes, drawn again until the scalar is in 1 .. n-1."""
    for _ in range(64):
        d = int.from_bytes(rand(32), "big")
        if 0 < d < P256_N:
            return ec.derive_private_key(d, ec.SECP256R1())
    raise RuntimeError("the random source keeps returning scalars out of range")


def to_pem(key: ec.EllipticCurvePrivateKey) -> bytes:
    return key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                             serialization.NoEncryption())


def from_pem(raw: bytes) -> ec.EllipticCurvePrivateKey:
    key = serialization.load_pem_private_key(raw, password=None)
    if not isinstance(key, ec.EllipticCurvePrivateKey) or not isinstance(key.curve, ec.SECP256R1):
        raise ValueError("the host key is not a P-256 key")
    return key
