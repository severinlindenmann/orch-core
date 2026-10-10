"""The suite (P-256 per D46), sealing, signatures, domain labels.

Spec: orch-relay ``docs/protocol-v2.md`` §1 (suite 2), §3 (labels), §4 (ids), §5 (SALTED-AEAD, sealing). Vectors:
``tests/vectors/vectors_v2.json`` (suite 2). No key is read from disk here (that is ``custody/``).
"""

from cryptography.exceptions import InvalidTag

from .encoding import EncodingError, b64u, hex_id, unb64u, unhex
from .labels import L, LABELS, signature_labels
from .sealing import SEAL_PURPOSES, open_sealed, seal, seal_aad
from .suite import (
    P256_N,
    PUB_LEN,
    SIG_LEN,
    SUITE_ID,
    CryptoError,
    aes_gcm_open,
    aes_gcm_seal,
    device_id,
    ecdh,
    generate_private_key,
    hkdf,
    person_id,
    pk_pin,
    private_key_from_scalar,
    private_scalar,
    public_bytes,
    salted_open,
    salted_seal,
    sha256,
    sign,
    validate_public_key,
    verify,
    wsk_pin,
)

__all__ = [
    "LABELS",
    "L",
    "P256_N",
    "PUB_LEN",
    "SEAL_PURPOSES",
    "SIG_LEN",
    "SUITE_ID",
    "CryptoError",
    "EncodingError",
    "InvalidTag",
    "aes_gcm_open",
    "aes_gcm_seal",
    "b64u",
    "device_id",
    "ecdh",
    "generate_private_key",
    "hex_id",
    "hkdf",
    "open_sealed",
    "person_id",
    "pk_pin",
    "private_key_from_scalar",
    "private_scalar",
    "public_bytes",
    "salted_open",
    "salted_seal",
    "seal",
    "seal_aad",
    "sha256",
    "sign",
    "signature_labels",
    "unb64u",
    "unhex",
    "validate_public_key",
    "verify",
    "wsk_pin",
]
