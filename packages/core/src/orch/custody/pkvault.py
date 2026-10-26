"""Prototype (run-9 feedback 6): the person key kept for hub steps, so a hub join, mode switch or leave is one click
and asks for no backup key. HARDENING SKIPPED: this is a new custody path (the person key at rest, 0600, this UID).

``orch setup`` writes the scalar here (``<state>/person-key/<ws>/devices/d_<person>.key``). A workspace without the
file keeps the old way: the backup key typed once in the page (or at the terminal).
"""

from __future__ import annotations

import contextlib
import os
from collections.abc import Callable
from pathlib import Path
from typing import Any

from orch import crypto

from .base import CustodyError
from .convenience import FileStore

__all__ = ["VaultSource", "has", "put"]


def _store(state_dir: str | os.PathLike[str]) -> FileStore:
    return FileStore(Path(state_dir) / "person-key")


def put(state_dir: str | os.PathLike[str], workspace_id: str, pk_pub: bytes, scalar: bytes) -> None:
    """Keep ``scalar`` (the person key of ``pk_pub``) for this workspace. Best effort: a failure never fails setup."""
    with contextlib.suppress(CustodyError, OSError, ValueError):
        store, dev = _store(state_dir), "d_" + crypto.person_id(pk_pub).hex()
        if not store.exists(workspace_id, dev):
            store.put(workspace_id, dev, bytes(scalar))


def has(state_dir: str | os.PathLike[str], workspace_id: str, person: str) -> bool:
    try:
        return _store(state_dir).exists(workspace_id, "d_" + person.removeprefix("p_"))
    except (CustodyError, OSError, ValueError):
        return False


class VaultSource:
    """The slice of ``PersonKeySource`` an owner proof needs: ``pk_pub`` and ``signer(label)`` (one proof)."""

    def __init__(self, state_dir: str | os.PathLike[str], workspace_id: str, person: str, **_: Any):
        self._key: Any = None
        self.person = person.removeprefix("p_")
        self._args = (state_dir, workspace_id)

    def __enter__(self) -> VaultSource:
        state_dir, workspace_id = self._args
        try:
            scalar = _store(state_dir).get(workspace_id, "d_" + self.person)
            self._key = crypto.private_key_from_scalar(int.from_bytes(scalar, "big"))
        except (CustodyError, OSError, crypto.CryptoError):
            raise CustodyError("no person key is kept for hub steps in this workspace") from None
        if crypto.person_id(crypto.public_bytes(self._key)).hex() != self.person:
            self._key = None
            raise CustodyError("the kept person key is not this person's")
        return self

    def __exit__(self, *exc: object) -> None:
        self._key = None

    @property
    def pk_pub(self) -> bytes:
        return crypto.public_bytes(self._key)

    def signer(self, label: str) -> Callable[[bytes], bytes]:
        key = self._key
        return lambda payload: crypto.sign(key, payload)
