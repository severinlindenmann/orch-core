"""Test doubles for the remote client: a hub wire, a software presence backend (tests only), a fake CTAP2 key."""

from __future__ import annotations

import hashlib
from typing import Any

from orch.crypto import suite
from orch.crypto.encoding import b64u
from orch.remoteclient.codes import ClientError
from orch.remoteclient.presence import Prompt, check_signable


class FakeWire:
    def __init__(self, incoming: list[dict[str, Any] | None] | None = None) -> None:
        self.incoming = list(incoming or [])
        self.sent: list[dict[str, Any]] = []
        self.closed = False

    def put(self, obj: dict[str, Any]) -> None:
        self.sent.append(obj)

    def get(self, timeout: float):
        if not self.incoming:
            return None
        o = self.incoming.pop(0)
        if o == "EOF":
            raise ClientError("remote_client.closed")
        return o

    def close(self) -> None:
        self.closed = True


class SoftPresence:
    """TEST ONLY: a software P-256 'platform' key behind the PresenceBackend seam. There is none in src."""

    kind = "platform"

    def __init__(self) -> None:
        self.key = suite.generate_private_key()
        self.pub = suite.public_bytes(self.key)
        self.prompts: list[Prompt] = []
        self.signed: list[bytes] = []
        self.cancel = False

    def create(self, name: str) -> dict[str, Any]:
        return {"kind": "platform", "pub65": self.pub, "ref": "a" * 32}

    def assert_(self, ref: str, msg: bytes, prompt: Prompt) -> dict[str, Any]:
        check_signable(msg)
        self.prompts.append(prompt)
        if self.cancel:
            raise ClientError("remote_client.cancelled")
        self.signed.append(msg)
        return {"form": "p256", "sig": b64u(suite.sign(self.key, msg))}


class FakeCtap:
    """A fake CTAP2 authenticator: ES256, UV capable, counting."""

    def __init__(
        self, *, uv: bool = True, pin: bool = False, alg: int = -7, flags: int = 0x05, counter: int = 0
    ) -> None:
        self.key = suite.generate_private_key()
        pub = suite.public_bytes(self.key)
        self.x, self.y = pub[1:33], pub[33:]
        self.options = {"uv": uv, "clientPin": pin}
        self.alg, self.flags, self.counter = alg, flags, counter
        self.made_flags = 0x45 | (0x04 if uv else 0)
        self.auth_extra = b""
        self.calls = 0

    def info(self) -> dict[str, Any]:
        return {"options": self.options, "algorithms": [self.alg]}

    def make_credential(self, cdh: bytes, user_name: str, prompt: Prompt) -> dict[str, Any]:
        return {"cred_id": b"c" * 32, "x": self.x, "y": self.y, "alg": self.alg, "flags": self.made_flags}

    def get_assertion(self, cdh: bytes, cred_id: bytes, prompt: Prompt) -> dict[str, Any]:
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import ec
        from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature

        self.calls += 1
        self.counter += 1
        auth = (
            hashlib.sha256(b"orch-remote").digest()
            + bytes([self.flags])
            + self.counter.to_bytes(4, "big")
            + self.auth_extra
        )
        raw = suite.sign(self.key, auth + cdh)
        der = encode_dss_signature(int.from_bytes(raw[:32], "big"), int.from_bytes(raw[32:], "big"))
        del hashes, ec
        return {"auth_data": auth, "sig": der}
