"""The relay outbox (§11 P3-D21, §11A.3, §11A.5): ``<state>/hosts/<ws>/outbox/``, written only by the store.

One file per entry, ``<n as 20 digits>.json`` (0600, the directory 0700), holding ``cj`` of::

    {"v": 1, "kind": "plain" | "restore", "n", "hash", "checkpoint", "restore": {...} | null, "catchup": bool}

``checkpoint`` is the exact WSK-signed workspace checkpoint the store wrote (for a ``restore``: the target T);
``hash`` is ``canon.checkpoint_hash`` of its ``o`` and ``n`` its number, so the file name, ``n`` and the checkpoint
agree. A ``restore`` entry carries the full evidence set in ``restore``: the source C (``source``), the signed
restore line (``event``), its ``log``, the signer's ``device_cert`` and ``pk_pub``, the workspace ``delegation`` and
the proof material (``proofs``, ``acc_from``, ``acc_before``, ``forward_heads``, ``abandoned_heads``,
``publication_complete``), exactly as the store's pending transition holds them.

Entries are ordered by ``n``: the store numbers every checkpoint above every existing number, so the order of the
numbers is the order of the appends. ``catchup.json`` in the same directory is the journal of a
:meth:`orch.store.Store.mint_catchup` replacement (written first, applied, then deleted).

Nothing here takes the store lock: :class:`orch.store.Store` calls it under the lock.
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from orch import canon

from .checkpoints import verify_object
from .errors import StoreError
from .fsio import fsync_dir, loads, read_or_none, write_atomic
from .rootfs import RootFS

__all__ = [
    "INTENT",
    "Outbox",
    "OutboxEntry",
    "RelayPosition",
    "Segment",
    "make_entry",
    "parse_entry",
    "pending_linked_appends",
]

_NAME = re.compile(r"([0-9]{20})\.json")
INTENT = "catchup.json"
_KINDS = ("plain", "restore")
_FIELDS = frozenset({"v", "kind", "n", "hash", "checkpoint", "restore", "catchup"})
RESTORE_KEYS = frozenset(
    {
        "source",
        "event",
        "log",
        "device_cert",
        "pk_pub",
        "delegation",
        "proofs",
        "acc_from",
        "acc_before",
        "forward_heads",
        "abandoned_heads",
        "publication_complete",
    }
)


@dataclass(frozen=True)
class Segment:
    """One log's part of a step set (§11A.5): the heads after ``after_seq`` (whose accumulator is ``from_acc``)."""

    log: str
    after_seq: int
    from_acc: str
    heads: tuple[str, ...]


@dataclass(frozen=True)
class RelayPosition:
    """Where a relay's held checkpoint H stands against this host (R3's startup classification, §11A.5), from
    :meth:`orch.store.Store.relay_position`. ``contained``: every entry of H is a point of the current logs (head and
    accumulator at its ``seq``); ``numbering``: the highest ``n`` the host ever reserved, signed or queued;
    ``current``: ``{n, hash}`` of the newest local workspace checkpoint (None when there is none); ``abandoned``: H is
    one of the workspace checkpoints a restore moved to ``.state/checkpoints/abandoned/``."""

    contained: bool
    numbering: int
    current: dict[str, Any] | None
    abandoned: bool


@dataclass(frozen=True)
class OutboxEntry:
    """A verified outbox entry. ``restore`` is the evidence set of a ``restore`` entry, else None."""

    n: int
    hash: str
    kind: str
    checkpoint: dict[str, Any]
    restore: dict[str, Any] | None
    catchup: bool

    def to_json(self) -> dict[str, Any]:
        return {
            "v": 1,
            "kind": self.kind,
            "n": self.n,
            "hash": self.hash,
            "checkpoint": self.checkpoint,
            "restore": self.restore,
            "catchup": self.catchup,
        }

    def to_bytes(self) -> bytes:
        return canon.cj_checked(self.to_json()) + b"\n"


def make_entry(
    checkpoint: dict[str, Any], *, restore: dict[str, Any] | None = None, catchup: bool = False
) -> OutboxEntry:
    o = checkpoint["o"]
    return OutboxEntry(
        n=o["n"],
        hash=canon.checkpoint_hash(o),
        kind="restore" if restore is not None else "plain",
        checkpoint=checkpoint,
        restore=restore,
        catchup=catchup,
    )


def parse_entry(
    raw: bytes | Mapping[str, Any], *, wsk_pub: bytes | None, workspace_id: str, genesis: str | None
) -> OutboxEntry:
    """The entry ``raw`` holds, after every check; :class:`StoreError` ``store.torn_write`` otherwise: the shape, the
    checkpoint's WSK signature, workspace, genesis and kind, ``n`` and ``hash`` equal to the checkpoint's, and for a
    restore the evidence keys and a target that holds the restore line."""
    try:
        d = loads(raw) if isinstance(raw, bytes) else raw
        if not isinstance(d, dict) or set(d) != _FIELDS:
            raise ValueError("shape")
        if d["v"] != 1 or d["kind"] not in _KINDS or type(d["catchup"]) is not bool or type(d["n"]) is not int:
            raise ValueError("fields")
        cp = d["checkpoint"]
        if wsk_pub is None or not verify_object(wsk_pub, cp):
            raise ValueError("the checkpoint is not signed by the workspace key")
        o = cp["o"]
        if o["kind"] != "workspace_checkpoint" or o["workspace_id"] != workspace_id or o["genesis"] != genesis:
            raise ValueError("another workspace, genesis or kind")
        if o["n"] != d["n"] or canon.checkpoint_hash(o) != d["hash"]:
            raise ValueError("n or hash")
        restore = d["restore"]
        if d["kind"] == "restore":
            if not isinstance(restore, dict) or set(restore) != RESTORE_KEYS or d["catchup"]:
                raise ValueError("restore evidence")
            ev = restore["event"]
            if not isinstance(ev, dict) or ev.get("type") != "restore":
                raise ValueError("restore line")
            entry = o["workspace_log"] if restore["log"] == "workspace" else o["tickets"].get(restore["log"])
            if not entry or entry["seq"] != ev.get("seq") or entry["head"] != canon.event_head(ev):
                raise ValueError("the target does not hold the restore line")
        elif restore is not None:
            raise ValueError("a plain entry carries no restore")
    except (ValueError, KeyError, TypeError, AttributeError, canon.HashError) as e:
        raise StoreError("store.torn_write", f"unreadable outbox entry ({e})") from None
    return OutboxEntry(d["n"], d["hash"], d["kind"], cp, restore, d["catchup"])


class Outbox:
    def __init__(self, directory: Path) -> None:
        self.dir = directory

    def path(self, n: int) -> Path:
        if type(n) is not int or not 0 < n < 10**20:
            raise StoreError("validation.outbox", "an outbox n is a positive integer")
        return self.dir / f"{n:020d}.json"

    def names(self) -> list[tuple[int, Path]]:
        """``(n, path)`` of every entry file, by ``n`` (temp files and the journal are not entries)."""
        if not self.dir.is_dir():
            return []
        out = []
        for p in self.dir.iterdir():
            m = _NAME.fullmatch(p.name)
            if m:
                out.append((int(m.group(1)), p))
        return sorted(out)

    def max_n(self) -> int:
        return max((n for n, _ in self.names()), default=0)

    def read(
        self, *, wsk_pub: bytes | None, workspace_id: str, genesis: str | None, limit: int | None = None
    ) -> list[OutboxEntry]:
        """The entries, lowest ``n`` first, each verified; only the first ``limit`` when given (R3 publishes the
        lowest entry first and need not verify the whole queue)."""
        out: list[OutboxEntry] = []
        for n, p in self.names():
            if limit is not None and len(out) >= limit:
                break
            if p.is_symlink():
                raise StoreError("store.torn_write", f"outbox entry {n} is a symlink")
            raw = read_or_none(p, None)
            if raw is None:
                continue
            entry = parse_entry(raw, wsk_pub=wsk_pub, workspace_id=workspace_id, genesis=genesis)
            if entry.n != n:
                raise StoreError("store.torn_write", f"outbox entry {p.name} holds n {entry.n}")
            out.append(entry)
        return out

    def ensure(self) -> None:
        if self.dir.is_symlink():
            raise StoreError("store.torn_write", "the outbox directory is a symlink")
        self.dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(self.dir, 0o700)

    def write(self, entry: OutboxEntry) -> None:
        """Write ``entry`` durably (temp + fsync + rename + directory fsync); an identical rewrite is harmless."""
        self.write_raw(entry.n, entry.to_bytes())

    def write_raw(self, n: int, data: bytes) -> None:
        self.ensure()
        write_atomic(self.path(n), data, mode=0o600)

    def delete(self, n: int) -> bool:
        try:
            os.unlink(self.path(n))
        except FileNotFoundError:
            return False
        fsync_dir(self.dir)
        return True

    # -- the catch-up journal
    @property
    def _intent(self) -> Path:
        return self.dir / INTENT

    def write_intent(self, data: dict[str, Any]) -> None:
        self.ensure()
        write_atomic(self._intent, canon.cj_checked(data) + b"\n", mode=0o600)

    def read_intent(self) -> dict[str, Any] | None:
        if self._intent.is_symlink():
            raise StoreError("store.torn_write", "the outbox catch-up journal is a symlink")
        raw = read_or_none(self._intent, None)
        if raw is None:
            return None
        try:
            d = loads(raw)
        except ValueError:
            raise StoreError("store.torn_write", "unreadable outbox catch-up journal") from None
        if not isinstance(d, dict):
            raise StoreError("store.torn_write", "unreadable outbox catch-up journal")
        return d

    def drop_intent(self) -> None:
        try:
            os.unlink(self._intent)
        except FileNotFoundError:
            return
        fsync_dir(self.dir)


def pending_linked_appends(fs: RootFS) -> list[tuple[str, str]]:
    """``(event id, log)`` of every crashed append under ``<root>/.state/pending`` whose manifest carries an outbox
    entry (a relay-linked workspace): for the doctor. Unreadable manifests are skipped (the store's open reports them).
    Read below the store's pinned root (``fs``, #516: a symlinked directory is none of them).
    Here, not in :mod:`orch.ops.health`, because op modules reach files only through ``ctx.caps`` (D-P2-9)."""
    import json

    out: list[tuple[str, str]] = []
    for name in sorted(fs.listdir_or_empty(".state/pending")):
        try:
            raw = fs.read_or_none(f".state/pending/{name}/manifest.json", 2_000_000)
            if raw is None:
                continue
            m = json.loads(raw)
            if isinstance(m, dict) and m.get("outbox") is not None:
                out.append((str(m["id"]), str(m["log"])))
        except (OSError, ValueError, KeyError, TypeError, StoreError):
            continue
    return out
