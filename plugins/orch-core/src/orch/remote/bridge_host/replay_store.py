"""The durable request store (spec §5.3): one record per request id, persisted before the request runs and before any
refusal issued after its signature verified is sent, with the per-device sequence state beside it.

    rid -> {device id, digest = H(header || ciphertext || tag), received_at, until, outcome}
    until = received_at + 900 s, from the host's clock; never from the sender's ts_ms

`outcome` is None (accepted, not finished: still running, or the host stopped before it finished), a refusal
({"refusal": code, ...}) or a response head with at most 64 KiB of body. A record lives until `until`, refusals
included. The store is bounded (MAX_RECORDS); when it is full of unexpired records nothing new can be recorded, and
a request that cannot be recorded never runs. A damaged record or sequence state raises files.Damaged, and the host
then answers in the way that can never run the request again.
"""
from __future__ import annotations

import base64
import binascii
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path

from orch.remote.bridge_host import files

RETENTION_MS = 900_000
MAX_RECORDS = 4096  # ponytail: one file per record, a directory scan to count; an index if this ever needs to grow
MAX_REPLAY_BODY = 64 * 1024
MAX_HEAD = 16 * 1024  # the canonical JSON of a stored outcome
_RECORD_LIMIT = 128 * 1024
_SEQ_LIMIT = 256
_PRUNE_EVERY_MS = 60_000
_ID = re.compile(r"[0-9a-f]{32}")
U64 = 1 << 64


class StoreFull(Exception):
    """MAX_RECORDS unexpired records exist: nothing new can be recorded, so nothing new may run."""


@dataclass(frozen=True)
class Record:
    device: str
    digest: str
    received_at: int
    until: int
    outcome: dict | None = None
    body: bytes = b""


def _int(v) -> bool:
    return type(v) is int and 0 <= v < U64


def _decode(raw: bytes) -> Record:
    try:
        d = json.loads(raw.decode("utf-8"))
        body = base64.b64decode(d["body"], validate=True) if d.get("body") else b""
        rec = Record(d["device"], d["digest"], d["received_at"], d["until"], d["outcome"], body)
    except (UnicodeDecodeError, ValueError, KeyError, TypeError, AttributeError, binascii.Error) as e:
        raise files.Damaged("a request record is not valid") from e
    if (d.get("v") != 1 or not isinstance(rec.device, str) or not _ID.fullmatch(rec.device)
            or not isinstance(rec.digest, str) or not re.fullmatch(r"[0-9a-f]{64}", rec.digest)
            or not _int(rec.received_at) or rec.until != rec.received_at + RETENTION_MS
            or not (rec.outcome is None or isinstance(rec.outcome, dict)) or len(rec.body) > MAX_REPLAY_BODY):
        raise files.Damaged("a request record is not valid")
    return rec


def _encode(rec: Record) -> bytes:
    if rec.outcome is not None and len(json.dumps(rec.outcome, sort_keys=True)) > MAX_HEAD:
        raise ValueError("a stored outcome head is at most 16 KiB")
    if len(rec.body) > MAX_REPLAY_BODY:
        raise ValueError("a stored replay body is at most 64 KiB")
    return json.dumps({"v": 1, "device": rec.device, "digest": rec.digest, "received_at": rec.received_at,
                       "until": rec.until, "outcome": rec.outcome,
                       "body": base64.b64encode(rec.body).decode("ascii")}, sort_keys=True).encode("utf-8")


class ReplayStore:
    """`root` is the workspace's bridge directory (files.bridge_dir). Not thread-safe: the host serialises calls."""

    def __init__(self, root: Path, max_records: int = MAX_RECORDS):
        self.dir = files.ensure_dir(Path(root) / "requests")
        self.seq_dir = files.ensure_dir(Path(root) / "seq")
        self.max_records = max_records
        self._pruned_at: int | None = None

    def _path(self, rid: str) -> Path:
        if not isinstance(rid, str) or not _ID.fullmatch(rid):
            raise ValueError("a request id is 32 lower-case hex characters")
        return self.dir / f"{rid}.json"

    def get(self, rid: str, now_ms: int) -> Record | None:
        """The unexpired record of `rid`, or None (an expired one is removed). Damaged for a record that cannot be
        trusted."""
        path = self._path(rid)
        raw = files.read(path, _RECORD_LIMIT)
        if raw is None:
            return None
        rec = _decode(raw)
        if now_ms >= rec.until:
            path.unlink(missing_ok=True)
            return None
        return rec

    def record(self, rid: str, device: str, digest: str, now_ms: int, outcome: dict | None = None) -> Record:
        """Persist a new record received now. Durable when this returns; FileExistsError when the rid already has
        one, StoreFull when the store is full."""
        rec = Record(device, digest, now_ms, now_ms + RETENTION_MS, outcome)
        self.put(rid, rec, now_ms)
        return rec

    def put(self, rid: str, rec: Record, now_ms: int) -> None:
        if self._pruned_at is None or now_ms - self._pruned_at >= _PRUNE_EVERY_MS or self._count() >= self.max_records:
            self.prune(now_ms)
        if self._count() >= self.max_records:
            raise StoreFull()
        files.create_exclusive(self._path(rid), _encode(rec))

    def set_outcome(self, rid: str, outcome: dict | None, now_ms: int, body: bytes = b"") -> Record:
        """Replace the outcome of an unexpired record (a refusal issued at step 9, or the finished response).
        LookupError when there is no such record."""
        rec = self.get(rid, now_ms)
        if rec is None:
            raise LookupError("no record for this request id")
        new = Record(rec.device, rec.digest, rec.received_at, rec.until, outcome, body)
        files.replace(self._path(rid), _encode(new))
        return new

    def in_flight(self, device: str, now_ms: int, running_key: str) -> list[str]:
        """The unexpired records of `device` that are not finished (no outcome, or running)."""
        out = []
        for name in os.listdir(self.dir):
            if not (name.endswith(".json") and _ID.fullmatch(name[:-5])):
                continue
            try:
                rec = self.get(name[:-5], now_ms)
            except files.Damaged:
                continue  # answers already_done/unknown anyway, never runs
            if rec is not None and rec.device == device and (rec.outcome is None or running_key in rec.outcome):
                out.append(name[:-5])
        return out

    def _count(self) -> int:
        return sum(1 for n in os.listdir(self.dir) if n.endswith(".json"))

    def prune(self, now_ms: int) -> int:
        """Remove expired records and left-over temporary files; damaged records stay (and keep counting)."""
        removed = 0
        for name in os.listdir(self.dir):
            path = self.dir / name
            if name.startswith(".") and name.endswith(".tmp"):
                path.unlink(missing_ok=True)
                continue
            if not (name.endswith(".json") and _ID.fullmatch(name[:-5])):
                continue
            try:
                raw = files.read(path, _RECORD_LIMIT)
                if raw is not None and now_ms >= _decode(raw).until:
                    path.unlink(missing_ok=True)
                    removed += 1
            except files.Damaged:
                continue
        self._pruned_at = now_ms
        return removed

    # -- the sequence state of each device (§5.2): high and a 64-bit bitmap -------------------------------------------

    def _seq_path(self, device: str) -> Path:
        if not isinstance(device, str) or not _ID.fullmatch(device):
            raise ValueError("a device id is 32 lower-case hex characters")
        return self.seq_dir / f"{device}.json"

    def seq_state(self, device: str) -> tuple[int, int]:
        """(high, bitmap); (0, 0) for a device that never sent a request. Damaged for a state that cannot be trusted."""
        raw = files.read(self._seq_path(device), _SEQ_LIMIT)
        if raw is None:
            return 0, 0
        try:
            d = json.loads(raw.decode("utf-8"))
            high, bitmap = d["high"], d["bitmap"]
        except (UnicodeDecodeError, ValueError, KeyError, TypeError) as e:
            raise files.Damaged("a sequence state is not valid") from e
        if d.get("v") != 1 or not _int(high) or not _int(bitmap):
            raise files.Damaged("a sequence state is not valid")
        return high, bitmap

    def save_seq(self, device: str, high: int, bitmap: int) -> None:
        if not _int(high) or not _int(bitmap):
            raise ValueError("high and bitmap are unsigned 64-bit")
        files.replace(self._seq_path(device), json.dumps({"v": 1, "high": high, "bitmap": bitmap}).encode("utf-8"))
