from __future__ import annotations

import hashlib
import time
from collections.abc import Callable

from cryptography.hazmat.primitives.asymmetric import ec

from orch_hub.access import Access, AccessSnapshots
from orch_hub.audit import open_log
from orch_hub.channels import Channels, registration_object
from orch_hub.channels.service import totp_code
from orch_hub.contract import keystore as KS
from orch_hub.contract import suite as S
from orch_hub.keys import HubKeys

PW = "correct horse battery"


class Clock:
    def __init__(self, t: int = 1_800_000_000_000) -> None:
        self.t = t

    def __call__(self) -> int:
        return self.t

    def advance(self, ms: int) -> None:
        self.t += ms


def fresh_code(secret: bytes, clock: Clock) -> str:
    """Advance ``clock`` to the next 30 s step whose code the hub WILL accept, and return that code.

    The verifier accepts steps ``s-1, s, s+1`` and claims the HIGHEST matching one (replay protection: a code string,
    once used, is never accepted again). When two adjacent steps share a 6-digit code (about 1 in 10^6 per pair, the
    secret is random) a code typed at step ``p`` claims ``p+1``, so a naive "advance 30 s" helper then offers a step that
    is already spent and the sign-in fails ``bad_credentials`` (the CI flake in test_phones_pairing). So skip any step
    whose code equals a neighbour's: then the match is exactly ``s``, later than anything a call at ``s-1`` could claim."""
    clock.advance(30_000)
    while totp_code(secret, (s := clock() // 30_000)) in (totp_code(secret, s - 1), totp_code(secret, s + 1)):
        clock.advance(30_000)
    return totp_code(secret, s)


def wall_codes(
    secret: bytes,
    steps: tuple[int, ...] = (0,),
    *,
    left_s: float = 2.0,
    now: Callable[[], float] = time.time,
    sleep: Callable[[float], None] = time.sleep,
) -> list[str]:
    """Codes for a hub that runs on the REAL clock: one per offset in ``steps`` from the current 30 s step ``s``.

    Two failure classes are ruled out, so callers never do their own code math:
    * window crossing: at least ``left_s`` seconds of step ``s`` are left (otherwise wait for the next step), so the hub
      still sees the step the codes were made for, if the caller uses them within ``left_s``;
    * neighbour collision (see :func:`fresh_code`): a step is waited out when a code used in it equals the code of a
      step next to it, so the hub claims exactly the step the code was made for and never spends a later one."""
    while True:
        t = now()
        rest = 30 - t % 30
        if rest >= left_s:
            s = int(t) // 30
            used = [s + o for o in steps]
            codes = {c: totp_code(secret, c) for c in range(min(used) - 1, max(used) + 2)}
            if all(codes[c] not in (codes[c - 1], codes[c + 1]) for c in used):
                return [codes[c] for c in used]
        sleep(rest + 0.05)


class Env:
    def __init__(self, tmp_path):
        self.clock = Clock()
        self.keys = HubKeys.init(tmp_path / "hub", now_ms=self.clock)
        self.acc = Access(str(tmp_path / "hub" / "access.db"), now_ms=self.clock)
        self.audit = open_log(tmp_path / "hub", self.keys, now_ms=self.clock, dev_mode=True)
        self.svc = Channels(
            self.keys, self.acc, tmp_path / "hub" / "channels.db", now_ms=self.clock, audit=self.audit, dev_mode=True
        )
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
        """A fresh code: the next 30 s step the hub accepts (codes are single use; see :func:`fresh_code`)."""
        return fresh_code(self.secrets[u], self.clock)

    def sign_in(self, u: str) -> str:
        return self.svc.sign_in(u, PW, self.next_code(u))["token"]

    def db_count(self, table: str, user: str) -> int:
        return self.svc.db.q(f"SELECT COUNT(*) FROM {table} WHERE user_id=?", (user,))[0][0]

    def close(self) -> None:
        self.svc.close()
        self.acc.close()
        self.audit.store.close()


def new_channel() -> KS.ChannelSigner:
    return KS.ChannelSigner(ec.generate_private_key(ec.SECP256R1()))


def prove(signer: KS.ChannelSigner, begun: dict) -> dict:
    """The channel key signs the ``hub.channel_register`` object the hub returned (own label, §6.3)."""
    return signer.sign_object(begun["challenge"])


def register(env: Env, u: str, signer: KS.ChannelSigner | None = None):
    signer = signer or new_channel()
    token = env.sign_in(u)
    begun = env.svc.begin_registration(token, signer.public, "test")
    return signer, env.svc.complete_registration(begun["challenge_id"], prove(signer, begun))


__all__ = ["Env", "S", "fresh_code", "hashlib", "new_channel", "prove", "register", "registration_object", "wall_codes"]
