"""What the host remembers outside the workspace (§5.10, §5.11): ``<host state dir>/hosts/<workspace_id>/``.

* ``genesis``: the head of ``workspace.created`` (the trust root pin), one line. Written once; a different value is
  ``trust.genesis_mismatch``.
* ``revocations.jsonl``: effective PK-signed revocations and verification evidence, retained across restores.
* ``checkpoint.json``: last accepted workspace checkpoint.
* ``heads/`` and ``transitions/``: durable ancestry and source-bound restore transitions for later publication.

P1 limit (§12 O7): these are files the same OS user owns, like the workspace itself.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from orch import canon

from .errors import StoreError
from .fsio import loads, read_or_none, write_atomic

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
        out = {}
        for line in raw.splitlines():
            try:
                rec = loads(line)
            except ValueError:
                raise StoreError("store.torn_write", "unreadable revocation evidence") from None
            old = out.get(rec["device"])
            if old is None or (old["reason"] != "compromised" and rec["reason"] == "compromised"):
                out[rec["device"]] = rec
        return [out[k] for k in sorted(out)]

    def note_revocation(
        self, device: str, reason: str, revocation: dict[str, Any], at: str, device_cert: dict[str, Any], pk_pub: str
    ) -> None:
        """Durably retain the earliest evidence of the highest reason, including upgrades."""
        old = next((r for r in self.revocations() if r["device"] == device), None)
        if old and (old["reason"] == "compromised" or reason != "compromised"):
            return
        self.dir.mkdir(parents=True, exist_ok=True)
        records = self.revocations()
        records.append(
            {
                "at": at,
                "device": device,
                "reason": reason,
                "revocation": revocation,
                "device_cert": device_cert,
                "pk_pub": pk_pub,
            }
        )
        write_atomic(self._revocations, b"".join(canon.cj_checked(r) + b"\n" for r in records), mode=0o600)

    def held(self) -> dict[str, Any] | None:
        raw = read_or_none(self.dir / "checkpoint.json")
        return loads(raw) if raw else None

    def accept_checkpoint(self, cp: dict[str, Any]) -> None:
        write_atomic(self.dir / "checkpoint.json", canon.cj_checked(cp) + b"\n", mode=0o600)

    def note_head(self, log: str, seq: int, head: str, prev: str | None, acc: str) -> None:
        path = self.dir / "heads" / head[7:]
        if path.exists():
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        write_atomic(
            path, canon.cj_checked({"log": log, "seq": seq, "head": head, "prev": prev, "acc": acc}), mode=0o600
        )

    def branch(self, head: str, stop: int = 0) -> list[str] | None:
        heads = []
        last_seq = None
        while head:
            try:
                canon.parse_hash(head)
            except canon.HashError:
                return None
            raw = read_or_none(self.dir / "heads" / head[7:])
            if raw is None:
                return None
            try:
                node = loads(raw)
                if node["head"] != head or (last_seq is not None and node["seq"] != last_seq - 1):
                    return None
                last_seq = node["seq"]
                if node["seq"] <= stop:
                    break
                heads.append(node["head"])
                head = node["prev"]
            except (ValueError, KeyError, TypeError):
                return None
        return list(reversed(heads))
