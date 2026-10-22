from __future__ import annotations

import hashlib

from cryptography.hazmat.primitives.asymmetric import ec

from orch_hub.access import Access, AccessSnapshots
from orch_hub.channels import Channels, registration_digest
from orch_hub.channels.service import totp_code
from orch_hub.contract import keystore as KS
from orch_hub.contract import suite as S
from orch_hub.contract.canon import b64u
from orch_hub.keys import HubKeys

PW = "correct horse battery"


class Clock:
    def __init__(self, t: int = 1_800_000_000_000) -> None:
        self.t = t

    def __call__(self) -> int:
        return self.t

    def advance(self, ms: int) -> None:
        self.t += ms


class Env:
    def __init__(self, tmp_path):
        self.clock = Clock()
        self.keys = HubKeys.init(tmp_path / "hub", now_ms=self.clock)
        self.acc = Access(str(tmp_path / "hub" / "access.db"), now_ms=self.clock)
        self.svc = Channels(self.keys, self.acc, tmp_path / "hub" / "channels.db", now_ms=self.clock)
        self.snaps = AccessSnapshots(self.acc, self.keys, self.clock)
        self.secrets: dict[str, bytes] = {}

    def user(self, name: str, *, password: bool = True) -> str:
        u = self.keys.issue_uk(None)["o"]["user_id"]
        self.acc.add_user(u, name)
        if password:
            self.svc.set_password(u, PW)
            self.secrets[u] = self._secret(u)
        return u

    def _secret(self, u: str) -> bytes:
        import base64

        s = self.svc.new_totp(u)["secret"]
        return base64.b32decode(s + "=" * (-len(s) % 8))

    def code(self, u: str, offset: int = 0) -> str:
        return totp_code(self.secrets[u], self.clock() // 30_000 + offset)

    def next_code(self, u: str) -> str:
        """A fresh code: the next 30 s step (codes are single use)."""
        self.clock.advance(30_000)
        return self.code(u)

    def sign_in(self, u: str) -> str:
        return self.svc.sign_in(u, PW, self.next_code(u))["token"]

    def close(self) -> None:
        self.svc.close()
        self.acc.close()


def new_channel() -> KS.ChannelSigner:
    return KS.ChannelSigner(ec.generate_private_key(ec.SECP256R1()))


def prove(signer: KS.ChannelSigner, begun: dict) -> str:
    return b64u(signer.proof(unb64(begun["digest"])))


def unb64(x: str) -> bytes:
    from orch_hub.contract.canon import unb64u

    return unb64u(x, 32)


def register(env: Env, u: str, signer: KS.ChannelSigner | None = None):
    signer = signer or new_channel()
    token = env.sign_in(u)
    begun = env.svc.begin_registration(token, signer.public, "test")
    return signer, env.svc.complete_registration(begun["challenge_id"], prove(signer, begun))


__all__ = ["Env", "S", "hashlib", "new_channel", "prove", "register", "registration_digest"]
