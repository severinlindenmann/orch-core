"""What the host remembers outside the workspace (§5.10, §5.11): ``<host state dir>/hosts/<workspace_id>/``.

* ``genesis``: the head of ``workspace.created`` (the trust root pin), one line. Written once; a different value is
  ``trust.genesis_mismatch``.
* ``revocations.jsonl``: effective PK-signed revocations and verification evidence per ``(device, person)`` pair,
  retained across restores. A pair is either chain evidence (it occurred in accepted history; sticky, never evicted)
  or forwarded evidence (``"chain": false``; capped per person, per device and in total, see :meth:`note_revocation`).
  A stronger forwarded reason for a chain pair is kept beside it under ``upgrade``, never replacing it (§5.10).
* ``checkpoint.json``: last accepted workspace checkpoint.
* ``heads.sqlite``: durable ancestry, indexed by head.
* ``pending-transitions/``: recovery work; ``transitions/``: completed publication records.

P1 limit (§12 O7): these are files the same OS user owns, like the workspace itself.
"""

from __future__ import annotations

import os
import sqlite3
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from orch import canon
from orch.identity.certs import revoke_strength

from .errors import StoreError
from .fsio import loads, read_or_none, write_atomic

__all__ = [
    "MAX_FORWARDED_HARD",
    "MAX_FORWARDED_PER_DEVICE",
    "MAX_FORWARDED_PER_PERSON_HARD",
    "MAX_FORWARDED_PER_PERSON",
    "MAX_FORWARDED_TOTAL",
    "HostPins",
    "is_forwarded",
    "pair",
]

MAX_FORWARDED_PER_DEVICE = 8  # forwarded (not chain) evidence records per device id
MAX_FORWARDED_PER_PERSON = 64  # forwarded evidence records per revoking person, members included
MAX_FORWARDED_TOTAL = 1024  # forwarded evidence records in total (the eviction cap; H12b review lowered it from 4096)
# Absolute bounds (H12b review, Opus F2/D1): non-owner forwarded records never exceed MAX_FORWARDED_HARD, and every
# person, the owner included, has at most MAX_FORWARDED_PER_PERSON_HARD: at most (1536 + 256) x 6 KiB, about 10.5 MiB.
MAX_FORWARDED_HARD = 1536  # non-owner forwarded records in total (members over MAX_FORWARDED_TOTAL included)
MAX_FORWARDED_PER_PERSON_HARD = 256  # forwarded records per person, the pinned owner included
MAX_FORWARDED_RECORD_BYTES = 6144  # one forwarded record as stored (cj); a larger one is dropped


def pair(rec: dict[str, Any]) -> tuple[str, str]:
    """The ``(d_device, p_person)`` key of a noted revocation record: prefixed, unlike ``checkpoints._hex_pair``."""
    return rec["device"], "p_" + rec["revocation"]["o"]["person_id"]


def is_forwarded(rec: dict[str, Any]) -> bool:
    """A record held as forwarded evidence, without an occurrence in accepted history (P3): capped, never claimed by
    the log, carried by a restore only where the workspace associates its device with its person."""
    return rec.get("chain", True) is False


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
        # No size limit (review round 2, Opus D2): only the host writes this file, forwarded records are capped
        # (MAX_FORWARDED_*) and chain records grow only with accepted history (one per pair, each bounded by the
        # event line cap), so a fixed read limit could only turn a long history into store.torn_write.
        raw = read_or_none(self._revocations, None)
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
        is_member: Callable[[str], bool] | None = None,
        owner: str | None = None,
    ) -> bool:
        """Durably retain the earliest evidence of the highest reason per ``(device, person)`` pair. Returns whether
        this evidence is held afterwards (as the record, or as a chain pair's ``upgrade``).

        Chain evidence (``chain``: it occurred in accepted history) is sticky per pair: it promotes a forwarded record
        of the pair (a stronger forwarded reason moves to ``upgrade``) and is never evicted. Forwarded evidence never
        replaces or demotes a chain record: a stronger forwarded reason is kept under the record's ``upgrade``.
        A new forwarded record needs ``is_member(p_person)`` (the current roster, current or past members), evaluated
        at eviction time for every candidate. Caps: :data:`MAX_FORWARDED_PER_PERSON` per person (members included; a
        new record over it is dropped; the pinned card owner ``owner`` is exempt, so a genuine owner revocation is
        never dropped), then :data:`MAX_FORWARDED_PER_DEVICE` per device and
        :data:`MAX_FORWARDED_TOTAL` in total: a new non-member record never evicts anything (dropped when full); a
        member's record evicts the oldest non-member record, or is kept over the cap when there is none."""
        key = (device, "p_" + revocation["o"]["person_id"])
        records = self.revocations()
        old = next((r for r in records if pair(r) == key), None)
        new = {
            "at": at,
            "device": device,
            "reason": reason,
            "revocation": revocation,
            "device_cert": device_cert,
            "pk_pub": pk_pub,
        }
        stronger = old is None or revoke_strength(reason) > revoke_strength(old["reason"])
        if chain:
            if old is not None and not is_forwarded(old) and not stronger:
                return True  # same or weaker chain evidence: the earliest of the highest reason stays
            if old is not None:
                carry = old.get("upgrade") if not is_forwarded(old) else old
                if carry is not None and revoke_strength(carry["reason"]) > revoke_strength(reason):
                    new["upgrade"] = {k: carry[k] for k in ("at", "reason", "revocation", "device_cert", "pk_pub")}
        else:
            if old is not None and not is_forwarded(old):
                held = old.get("upgrade", old)
                if revoke_strength(reason) > revoke_strength(held["reason"]):
                    old["upgrade"] = new  # kept beside the chain record, never replacing it
                    self._write(records)
                return True
            if old is not None and not stronger:
                return True
            new["chain"] = False
            if len(canon.cj_checked(new)) > MAX_FORWARDED_RECORD_BYTES:
                return False  # forwarded evidence is bounded per record (H12b review, Opus F2)
            if old is None:
                if is_member is None:
                    raise TypeError("forwarded evidence needs is_member (the current roster)")
                room = self._make_room(records, device, key[1], is_member, owner)
                if room is None:
                    return False
                records = room
        records = [r for r in records if pair(r) != key]
        records.append(new)
        self._write(records)
        return True

    def _write(self, records: list[dict[str, Any]]) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        write_atomic(self._revocations, b"".join(canon.cj_checked(r) + b"\n" for r in records), mode=0o600)

    @staticmethod
    def _make_room(
        records: list[dict[str, Any]],
        device: str,
        person: str,
        is_member: Callable[[str], bool],
        owner: str | None = None,
    ) -> list[dict[str, Any]] | None:
        """``records`` with room for one more forwarded record of ``(device, person)``, or None to drop it."""
        forwarded = [r for r in records if is_forwarded(r)]
        if sum(pair(r)[1] == person for r in forwarded) >= MAX_FORWARDED_PER_PERSON_HARD:
            return None  # the absolute per-person bound: the owner too
        if person != owner and sum(pair(r)[1] == person for r in forwarded) >= MAX_FORWARDED_PER_PERSON:
            return None  # the per-person cap; the pinned owner is exempt
        member = is_member(person)
        for scope, cap in (
            ([r for r in forwarded if r["device"] == device], MAX_FORWARDED_PER_DEVICE),
            (forwarded, MAX_FORWARDED_TOTAL),
        ):
            if len(scope) < cap:
                continue
            if not member:
                return None  # a non-member never evicts: the new record is dropped
            victims = [r for r in scope if not is_member(pair(r)[1])]
            if victims:  # the oldest non-member record goes; members stay (bounded per person)
                gone = min(victims, key=lambda r: r["at"])
                records = [r for r in records if r is not gone]
                forwarded = [r for r in forwarded if r is not gone]
        if person != owner and sum(pair(r)[1] != owner for r in forwarded) >= MAX_FORWARDED_HARD:
            return None  # the absolute total bound over non-owner records; the owner has its own 256 (review D1)
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
        """The checkpoint numbering high-water mark (0 if none): the highest ``n`` ever reserved (:meth:`reserve_n`) or
        accepted. Read from ``checkpoint-n`` on every call (one small file; another store may have reserved since); a
        host that predates the file falls back to its held checkpoint."""
        raw = read_or_none(self.dir / "checkpoint-n")
        try:
            mark = int(raw) if raw else 0
        except ValueError:
            raise StoreError("store.torn_write", "unreadable checkpoint high-water mark") from None
        if not raw:
            held = self.held()
            mark = held["o"]["n"] if held and "o" in held else 0
        self._mark = mark
        return mark

    def reserve_n(self, n: int) -> None:
        """Reserve checkpoint number ``n`` durably (H12b review, Opus F1).

        **Invariant: no workspace checkpoint ``n`` is signed unless it is reserved first, and a reserved ``n`` is never
        reused.** The store calls this before it signs anything numbered ``n`` (an append's planned checkpoint, any
        other workspace checkpoint, a restore target, catch-up prefixes), so ``high_n()`` is at least the ``n`` of
        everything that was ever signed, wherever the bytes went (the outbox, the relay, a crashed pending
        directory), and the next number is read from this one value. A number burned by a crash costs nothing.
        Never lowers the mark."""
        if type(n) is not int or n < 1:
            raise StoreError("validation.outbox", "a checkpoint n is a positive integer")
        if n > self.high_n():
            self.dir.mkdir(parents=True, exist_ok=True)
            write_atomic(self.dir / "checkpoint-n", str(n).encode() + b"\n", mode=0o600)
            self._mark = n

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
