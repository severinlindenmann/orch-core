"""What the host remembers outside the workspace (§5.10, §5.11): ``<host state dir>/hosts/<workspace_id>/``.

* ``genesis``: the head of ``workspace.created`` (the trust root pin), one line. Written once; a different value is
  ``trust.genesis_mismatch``.
* ``revocations.jsonl``: every person-key-signed device revocation the host appended (``device.revoked`` payload as
  ``cj``), so that a workspace ``restore`` can put them back (§5.10 "Restore never drops revocations").

P1 limit (§12 O7): these are files the same OS user owns, like the workspace itself.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from orch import canon

from .errors import StoreError
from .fsio import append_durable, read_or_none, write_atomic

__all__ = ["HostPins"]


class HostPins:
    def __init__(self, state_dir: str | os.PathLike[str], workspace_id: str) -> None:
        self.dir = Path(state_dir) / "hosts" / workspace_id

    @property
    def _genesis(self) -> Path:
        return self.dir / "genesis"

    @property
    def _revocations(self) -> Path:
        return self.dir / "revocations.jsonl"

    def genesis(self) -> str | None:
        raw = read_or_none(self._genesis)
        if raw is None:
            return None
        text = raw.decode("ascii", "replace").strip()
        try:
            canon.parse_hash(text)
        except canon.HashError:
            raise StoreError("trust.genesis_mismatch", "the genesis pin file is unreadable") from None
        return text

    def pin_genesis(self, genesis: str) -> None:
        """Pin ``genesis``; a pin that exists must be the same (never overwritten)."""
        have = self.genesis()
        if have is not None:
            if have != genesis:
                raise StoreError("trust.genesis_mismatch", "the genesis differs from the pinned one")
            return
        self.dir.mkdir(parents=True, exist_ok=True)
        write_atomic(self._genesis, genesis.encode("ascii") + b"\n", mode=0o600)

    def revocations(self) -> list[dict[str, Any]]:
        raw = read_or_none(self._revocations)
        if not raw:
            return []
        out = []
        for line in raw.splitlines():
            try:
                out.append(json.loads(line))
            except ValueError:
                continue
        return out

    def note_revocation(self, device: str, reason: str, revocation: dict[str, Any], at: str) -> None:
        """Remember a revocation (``at``: when the host appended it). Once noted it is never dropped or changed."""
        if any(r.get("device") == device for r in self.revocations()):
            return
        self.dir.mkdir(parents=True, exist_ok=True)
        line = canon.cj_checked({"at": at, "device": device, "reason": reason, "revocation": revocation}) + b"\n"
        append_durable(self._revocations, line)
