"""Keys, sealing, ids and MACs (spec §2, §3.3, §8). Every label ends in its version, so a later version derives
unrelated keys."""
from __future__ import annotations

import base64
import hashlib
import hmac

from orch.remote.bridge_host import CRYPTO_HINT, MissingCryptography
from orch.remote.bridge_host.envelope import Header

try:
    from cryptography.exceptions import InvalidTag
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    from cryptography.hazmat.primitives.kdf.hkdf import HKDF
except ImportError as e:  # pragma: no cover - exercised by test_bridge_host_without_cryptography
    raise MissingCryptography(CRYPTO_HINT) from e

L_WS = b"sharing/bridge/ws/v1|"
L_MSG = b"sharing/bridge/msg/v1"
L_DEVICE = b"sharing/bridge/device/v1|"
L_FP = b"sharing/bridge/fp/v1|"
L_HOST = b"sharing/bridge/host/v1|"
L_PAIR = b"sharing/bridge/pair/v1|"
L_PHONE = b"sharing/bridge/phone-link/v1|"
ZERO_NONCE = bytes(12)  # safe only because every K_msg seals exactly one plaintext (§2.3)

__all__ = ["InvalidTag", "hkdf", "workspace_key", "message_key", "seal", "open_sealed", "device_id",
           "device_fingerprint", "host_pin", "pair_mac", "phone_link_proof"]


def hkdf(ikm: bytes, salt: bytes, info: bytes, length: int = 32) -> bytes:
    """RFC 5869 HKDF-SHA-256; an empty salt is HashLen zero bytes (§2.2), as in WebCrypto."""
    return HKDF(hashes.SHA256(), length, salt or None, info).derive(ikm)


def workspace_key(mk: bytes, workspace_hex: str) -> bytes:
    """K_ws from the master key. The host itself never derives this (it is handed K_ws); kept for the vectors."""
    if len(mk) != 32 or len(workspace_hex) != 32:
        raise ValueError("MK is 32 bytes and the workspace 32 hex characters")
    return hkdf(mk, b"", L_WS + workspace_hex.encode("ascii"))


def message_key(k_ws: bytes, salt: bytes) -> bytes:
    """K_msg for one envelope, from the header's 16-byte random salt. AES-256 only (§2.3)."""
    if len(k_ws) != 32 or len(salt) != 16:
        raise ValueError("K_ws is 32 bytes and the salt 16")
    return hkdf(k_ws, salt, L_MSG)


def seal(k_ws: bytes, header: bytes, plaintext: bytes) -> bytes:
    """ciphertext || tag, AES-256-GCM under K_msg with the zero nonce and the whole 104-byte header as AAD."""
    return AESGCM(message_key(k_ws, Header.decode(header).salt)).encrypt(ZERO_NONCE, plaintext, header)


def open_sealed(k_ws: bytes, header: bytes, body: bytes) -> bytes:
    """The plaintext, or InvalidTag."""
    return AESGCM(message_key(k_ws, Header.decode(header).salt)).decrypt(ZERO_NONCE, body, header)


def device_id(workspace: bytes, pub: bytes) -> bytes:
    """Per workspace, so one browser key gives a different id in each workspace (§2.4)."""
    return hashlib.sha256(L_DEVICE + workspace + pub).digest()[:16]


def device_fingerprint(pub: bytes) -> str:
    """The relay app's 100-bit fingerprint format (base32, 4-4-4-4-4) under the bridge's own label."""
    raw = base64.b32encode(hashlib.sha256(L_FP + pub).digest()).decode("ascii")[:20]
    return "-".join(raw[i:i + 4] for i in range(0, 20, 4))


def host_pin(host_pub: bytes) -> bytes:
    return hashlib.sha256(L_HOST + host_pub).digest()


def pair_mac(secret: bytes, workspace: bytes, pairing_id: bytes, pub: bytes) -> bytes:
    return hmac.new(secret, L_PAIR + workspace + pairing_id + pub, hashlib.sha256).digest()


def phone_link_proof(phone_key: bytes, dev_id: bytes) -> bytes:
    return hmac.new(phone_key, L_PHONE + dev_id, hashlib.sha256).digest()
