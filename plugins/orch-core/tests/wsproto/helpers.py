"""Test helpers for the neutral protocol modules and the epoch key store."""

from __future__ import annotations

from orch import crypto

from . import corpus

WS = corpus.IDS["workspace_a"]
WSK_PRIV = corpus.priv("wsk_a.sig")
T0 = 1_790_000_000_000


class Key:
    def __init__(self, priv=WSK_PRIV):
        self.priv, self.count = priv, 0

    @property
    def public_key(self) -> bytes:
        return crypto.public_bytes(self.priv)

    def sign(self, payload: bytes) -> bytes:
        self.count += 1
        return crypto.sign(self.priv, payload)


class MemLedger:
    """An in-memory :class:`orch.wsproto.state.SignLedger` (the file-backed one is tested in test_state)."""

    def __init__(self):
        self.entries: dict[tuple[str, str], tuple[str, str]] = {}

    def get(self, kind, key):
        return self.entries.get((kind, key))

    def keys(self, kind):
        return sorted(k for (kd, k) in self.entries if kd == kind)

    def record(self, kind, key, digest, sig):
        self.entries[(kind, key)] = (digest, sig)


class Alarms(list):
    def __call__(self, code: str, detail: str) -> None:
        self.append((code, detail))

    def codes(self):
        return [c for c, _ in self]
