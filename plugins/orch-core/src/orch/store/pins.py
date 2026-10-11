"""What the host remembers outside the workspace (§5.10, §5.11): ``<host state dir>/hosts/<workspace_id>/``.

* ``genesis``: the head of ``workspace.created`` (the trust root pin), one line. Written once; a different value is
  ``trust.genesis_mismatch``.
* ``revocations.jsonl``: effective PK-signed revocations and verification evidence per ``(device, person)`` pair,
  retained across restores. Evidence from accepted history (``chain``) is never evicted. Other (forwarded) evidence
  is capped per device and in total; evidence of a person who is not a current or past member goes first, and a
  member's evidence is never evicted by a non-member's (§5.10).
* ``checkpoint.json``: last accepted workspace checkpoint.
* ``heads.sqlite``: durable ancestry, indexed by head.
* ``pending-transitions/``: recovery work; ``transitions/``: completed publication records.

P1 limit (§12 O7): these are files the same OS user owns, like the workspace itself.
"""

from __future__ import annotations

import os
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from orch import canon

from .errors import StoreError
from .fsio import loads, read_or_none, write_atomic

__all__ = ["MAX_FORWARDED_PER_DEVICE", "MAX_FORWARDED_TOTAL", "HostPins", "pair"]

MAX_FORWARDED_PER_DEVICE = 8  # forwarded (not chain) evidence records per device id
MAX_FORWARDED_TOTAL = 4096  # forwarded evidence records in total


def pair(rec: dict[str, Any]) -> tuple[str, str]:
    """The ``(d_device, p_person)`` key of a noted revocation record."""
    return rec["device"], "p_" + rec["revocation"]["o"]["person_id"]


class HostPins:
    def __init__(self, state_dir: str | os.PathLike[str], workspace_id: str) -> None:
        self.dir = Path(state_dir) / "hosts" / workspace_id
        self._db: sqlite3.Connection | None = None
        self._mark: int | None = None  # cached checkpoint high-water mark (monotonic, one writer under the lock)
        self._prior_heads = self._had_heads()  # recorded ancestry from an earlier run, before this one adds rows

    def _had_heads(self) -> bool:
        path = self.dir / "heads.sqlite"
        if not path.is_file():
            return False
        try:
            db = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
            try:
                return db.execute("SELECT 1 FROM heads LIMIT 1").fetchone() is not None
            finally:
                db.close()
        except sqlite3.Error:
            return True  # unreadable ancestry is still evidence that this host kept state

    def checkpoint_evidence(self) -> bool:
        """Whether this host state dir shows that checkpoints existed: a high-water mark, ancestry recorded by an
        earlier run, or a published transition. Without any, a missing checkpoint is a new host (§5.10)."""
        return (
            (self.dir / "checkpoint-n").is_file() or self._prior_heads or any((self.dir / "transitions").glob("*.json"))
        )

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
        """The effective evidence per ``(device, person)`` pair (§5.10), sorted by the pair: another person's evidence
        for the same device id is a separate record, never merged into or replacing this one."""
        raw = read_or_none(self._revocations)
        if not raw:
            return []
        out = {}
        for line in raw.splitlines():
            try:
                rec = loads(line)
            except ValueError:
                raise StoreError("store.torn_write", "unreadable revocation evidence") from None
            try:
                key = pair(rec)
            except (KeyError, TypeError):
                raise StoreError("store.torn_write", "unreadable revocation evidence") from None
            old = out.get(key)
            if old is None or (old["reason"] != "compromised" and rec["reason"] == "compromised"):
                out[key] = rec
        return [out[k] for k in sorted(out)]

    def note_revocation(
        self,
        device: str,
        reason: str,
        revocation: dict[str, Any],
        at: str,
        device_cert: dict[str, Any],
        pk_pub: str,
        *,
        chain: bool = True,
        member: bool = True,
    ) -> bool:
        """Durably retain the earliest evidence of the highest reason per ``(device, person)``, including upgrades.

        ``chain`` evidence (from accepted history) is always kept. Forwarded evidence (``chain=False``) is capped at
        :data:`MAX_FORWARDED_PER_DEVICE` per device and :data:`MAX_FORWARDED_TOTAL` in total: over a cap the oldest
        forwarded non-member record goes first; a member's evidence (``member``: the person is a current or past
        member) is never evicted for a non-member's, and a non-member record that finds nothing to evict is dropped.
        Returns whether the evidence is held afterwards."""
        key = (device, "p_" + revocation["o"]["person_id"])
        records = self.revocations()
        old = next((r for r in records if pair(r) == key), None)
        if old and (old["reason"] == "compromised" or reason != "compromised"):
            return True
        new = {
            "at": at,
            "device": device,
            "reason": reason,
            "revocation": revocation,
            "device_cert": device_cert,
            "pk_pub": pk_pub,
        }
        if not chain:
            new["chain"], new["member"] = False, member
        records = [r for r in records if pair(r) != key]  # an upgrade replaces the pair's weaker record
        if not chain:
            records = self._make_room(records, device, member)
            if records is None:
                return False
        records.append(new)
        self.dir.mkdir(parents=True, exist_ok=True)
        write_atomic(self._revocations, b"".join(canon.cj_checked(r) + b"\n" for r in records), mode=0o600)
        return True

    @staticmethod
    def _make_room(records: list[dict[str, Any]], device: str, member: bool) -> list[dict[str, Any]] | None:
        """``records`` with room for one more forwarded record of ``device``, or None when it must be dropped."""

        def forwarded(r: dict[str, Any]) -> bool:
            return r.get("chain", True) is False

        def evict(scope: list[dict[str, Any]]) -> list[dict[str, Any]] | None:
            victims = [r for r in scope if forwarded(r) and not r.get("member", True)]
            if not victims and member:
                return records  # a member's evidence is kept even over the cap; never evict another member's
            if not victims:
                return None
            gone = min(victims, key=lambda r: r["at"])
            return [r for r in records if r is not gone]

        mine = [r for r in records if r["device"] == device and forwarded(r)]
        if len(mine) >= MAX_FORWARDED_PER_DEVICE:
            got = evict(mine)
            if got is None:
                return None
            records = got
        if sum(forwarded(r) for r in records) >= MAX_FORWARDED_TOTAL:
            got = evict(records)
            if got is None:
                return None
            records = got
        return records

    def held(self) -> dict[str, Any] | None:
        raw = read_or_none(self.dir / "checkpoint.json")
        return loads(raw) if raw else None

    def accept_checkpoint(self, cp: dict[str, Any]) -> None:
        n = cp["o"]["n"]
        if n > self.high_n():  # the durable high-water mark: n never restarts below it
            write_atomic(self.dir / "checkpoint-n", str(n).encode() + b"\n", mode=0o600)
            self._mark = n
        write_atomic(self.dir / "checkpoint.json", canon.cj_checked(cp) + b"\n", mode=0o600)

    def high_n(self) -> int:
        """The highest workspace checkpoint ``n`` this host ever accepted (0 if none). Read from ``checkpoint-n`` once
        and cached; a host that predates the file falls back to its held checkpoint, once."""
        if self._mark is None:
            raw = read_or_none(self.dir / "checkpoint-n")
            try:
                mark = int(raw) if raw else 0
            except ValueError:
                raise StoreError("store.torn_write", "unreadable checkpoint high-water mark") from None
            if not raw:
                held = self.held()
                mark = held["o"]["n"] if held and "o" in held else 0
            self._mark = mark
        return self._mark

    @contextmanager
    def _heads(self) -> Iterator[sqlite3.Connection]:
        if self._db is None:
            self.dir.mkdir(parents=True, exist_ok=True)
            self._db = sqlite3.connect(self.dir / "heads.sqlite")
            self._db.execute("PRAGMA synchronous=FULL")  # ancestry survives a crash; unlike the derived index
            self._db.execute("CREATE TABLE IF NOT EXISTS heads (head TEXT PRIMARY KEY, seq INTEGER, prev TEXT)")
        with self._db:
            yield self._db

    def close(self) -> None:
        if self._db is not None:
            self._db.close()
            self._db = None

    def note_head(self, seq: int, head: str, prev: str | None) -> None:
        with self._heads() as db:
            db.execute("INSERT OR IGNORE INTO heads VALUES (?, ?, ?)", (head, seq, prev))

    def note_heads(self, heads: list[str], verified: int) -> None:
        """A single high-water lookup skips an already recorded branch on reload."""
        if not verified:
            return
        with self._heads() as db:
            if db.execute("SELECT 1 FROM heads WHERE head = ?", (heads[verified - 1],)).fetchone():
                return
            db.executemany(
                "INSERT OR IGNORE INTO heads VALUES (?, ?, ?)",
                ((heads[i], i + 1, heads[i - 1] if i else None) for i in range(verified)),
            )

    def branch(self, head: str, stop: int = 0) -> list[str] | None:
        heads = []
        last_seq = None
        with self._heads() as db:
            while head:
                row = db.execute("SELECT seq, prev FROM heads WHERE head = ?", (head,)).fetchone()
                if row is None:
                    return None
                seq, prev = row
                if last_seq is not None and seq != last_seq - 1:
                    return None
                if seq <= stop:
                    break
                heads.append(head)
                last_seq, head = seq, prev
        return list(reversed(heads))
