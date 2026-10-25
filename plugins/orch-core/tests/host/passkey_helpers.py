"""A software passkey for the signer's step-up tests (#1198 R1-6): its own tiny CBOR and WebAuthn assertion maker, so a
bug in ``orch.click.passkeys`` cannot shape what it is checked against."""

from __future__ import annotations

import base64
import hashlib
import json
import secrets
from datetime import UTC, datetime

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec

from orch.click.passkey_store import Passkey, PasskeyStore

RP = "orch.example.com"
UP, UV = 0x01, 0x04


def b64u(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def _head(major: int, n: int) -> bytes:
    if n < 24:
        return bytes([major << 5 | n])
    for info, size in ((24, 1), (25, 2)):
        if n < 1 << (8 * size):
            return bytes([major << 5 | info]) + n.to_bytes(size, "big")
    raise ValueError(n)


def _cbor(v) -> bytes:
    if isinstance(v, int):
        return _head(0, v) if v >= 0 else _head(1, -1 - v)
    if isinstance(v, bytes):
        return _head(2, len(v)) + v
    return _head(5, len(v)) + b"".join(_cbor(k) + _cbor(x) for k, x in v.items())


class Authenticator:
    def __init__(self, rp_id: str = RP):
        self.rp_id = rp_id
        self.key = ec.generate_private_key(ec.SECP256R1())
        self.credential_id = secrets.token_bytes(32)
        self.handle = secrets.token_bytes(32)
        self.count = 0

    def record(self, name: str = "Test, Chrome") -> Passkey:
        raw = self.key.public_key().public_bytes(
            serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint
        )
        cose = _cbor({1: 2, 3: -7, -1: 1, -2: raw[1:33], -3: raw[33:]})
        now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        return Passkey(
            credential_id=b64u(self.credential_id),
            user_handle=b64u(self.handle),
            public_key=b64u(cose),
            alg=-7,
            backup_eligible=False,
            name=name,
            created=now,
            last_used=None,
            sign_count=0,
        )

    def proof(self, nonce_b64: str, challenge: bytes, *, origin: str | None = None, flags: int = UP | UV) -> dict:
        """What the dashboard server relays: the nonce and the browser's assertion over ``challenge``."""
        cd = json.dumps(
            {
                "type": "webauthn.get",
                "challenge": b64u(challenge),
                "origin": origin or "https://" + self.rp_id,
                "crossOrigin": False,
            },
            separators=(",", ":"),
        ).encode()
        self.count += 1
        data = hashlib.sha256(self.rp_id.encode()).digest() + bytes([flags]) + self.count.to_bytes(4, "big")
        sig = self.key.sign(data + hashlib.sha256(cd).digest(), ec.ECDSA(hashes.SHA256()))
        return {
            "nonce": nonce_b64,
            "id": b64u(self.credential_id),
            "response": {
                "clientDataJSON": b64u(cd),
                "authenticatorData": b64u(data),
                "signature": b64u(sig),
                "userHandle": b64u(self.handle),
            },
        }


def store_with(directory, *authenticators) -> PasskeyStore:
    directory.mkdir(mode=0o700, exist_ok=True)
    store = PasskeyStore(directory / "passkeys.json")
    for a in authenticators:
        store.add(a.record())
    return store
