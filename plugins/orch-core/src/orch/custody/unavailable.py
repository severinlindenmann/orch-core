"""Backends that the portable-custody doc (§6 phasing) does not put in P1: a clear error, not a silent fallback."""

from __future__ import annotations

from .base import AUTH_VALUES, BackendUnavailable

__all__ = ["PLANNED", "UnavailableBackend"]

PLANNED = {
    "secure-enclave": "P1b (Apple-silicon Macs, user presence per signature, D41)",
    "secure-enclave-unlocked": "P3 (the iPhone app, D49; never a Mac or CLI backend)",
    "webauthn": "P2 (the dashboard)",
    "tpm": "later (Windows)",
    "windows-hello": "later (Windows)",
}
assert set(PLANNED) <= set(AUTH_VALUES)


class UnavailableBackend:
    """Placeholder that satisfies the interface and refuses every call with :class:`BackendUnavailable`."""

    person_capable = False

    def __init__(self, name: str) -> None:
        if name not in PLANNED:
            raise ValueError(name)
        self.name = name
        self.auth = name
        self._why = f"the {name} custody backend is not available in P1 (planned: {PLANNED[name]}); use 'passphrase'"

    def _no(self, *_a, **_k):
        raise BackendUnavailable(self._why)

    create = public_key = sign = exists = delete = _no

    def presence(self) -> str:
        return self.auth or "none"
