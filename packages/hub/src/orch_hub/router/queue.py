"""Persistent per-recipient envelope queue of the router (SQLite, a file or ``:memory:``).

Rows are opaque sealed envelopes (never logged). An acknowledged row keeps only its marker (ids and SHA-256, no bytes)
until it expires, so the same envelope handed over again is not queued or pushed a second time. Bounds per recipient: entries, bytes, and entries per sender; entries
expire after ``ttl_ms``. Order is the autoincrement id (enqueue order), which is the delivery order.
"""

from __future__ import annotations

import hashlib
import threading
from dataclasses import dataclass

from ..contract.hub import Refused
from ..fsutil import open_private_db

_SCHEMA = """
CREATE TABLE IF NOT EXISTS route_queue (
  id INTEGER PRIMARY KEY AUTOINCREMENT, to_ws TEXT NOT NULL, from_ws TEXT NOT NULL, env_id TEXT NOT NULL,
  digest BLOB NOT NULL, size INTEGER NOT NULL, enq_ms INTEGER NOT NULL, envelope BLOB NOT NULL,
  done INTEGER NOT NULL DEFAULT 0,
  UNIQUE (to_ws, from_ws, env_id)
)
"""


@dataclass(frozen=True)
class Limits:
    max_entries: int = 256  # per recipient
    max_bytes: int = 16 << 20  # per recipient
    max_per_sender: int = 64  # per (recipient, sender): one chatty peer cannot fill the whole queue
    ttl_ms: int = 31 * 24 * 3600 * 1000  # ws->ws envelopes carry a deadline of at most 30 d (protocol-v2 §11.4)


class RouteQueue:
    def __init__(self, path: str = ":memory:", limits: Limits | None = None) -> None:
        self.limits = limits or Limits()
        self._lock = threading.RLock()
        self._db = open_private_db(path, check_same_thread=False)
        self._db.executescript(_SCHEMA)
        self._db.commit()

    def close(self) -> None:
        with self._lock:
            self._db.close()

    def add(self, to_ws: str, from_ws: str, env_id: str, envelope: bytes, now_ms: int) -> int:
        """Queue an envelope; returns its queue id. The same ``(from, id)`` with the same bytes is already queued:
        returns the existing id, queued or already delivered (a sender may retry); other bytes under that pair -> ``replay``. A full queue ->
        ``queue_full``."""
        digest = hashlib.sha256(envelope).digest()
        with self._lock, self._db:
            self._expire(now_ms)
            row = self._db.execute(
                "SELECT id, digest FROM route_queue WHERE to_ws=? AND from_ws=? AND env_id=?", (to_ws, from_ws, env_id)
            ).fetchone()
            if row is not None:
                if row[1] != digest:
                    raise Refused("replay")
                return row[0]
            n, size = self._db.execute(
                "SELECT COUNT(*), COALESCE(SUM(size),0) FROM route_queue WHERE to_ws=? AND done=0", (to_ws,)
            ).fetchone()
            (mine,) = self._db.execute(
                "SELECT COUNT(*) FROM route_queue WHERE to_ws=? AND from_ws=? AND done=0", (to_ws, from_ws)
            ).fetchone()
            lim = self.limits
            if n >= lim.max_entries or size + len(envelope) > lim.max_bytes or mine >= lim.max_per_sender:
                raise Refused("queue_full")
            cur = self._db.execute(
                "INSERT INTO route_queue(to_ws, from_ws, env_id, digest, size, enq_ms, envelope) VALUES (?,?,?,?,?,?,?)",
                (to_ws, from_ws, env_id, digest, len(envelope), now_ms, envelope),
            )
            return int(cur.lastrowid)

    def head(self, to_ws: str, now_ms: int) -> tuple[int, bytes] | None:
        with self._lock, self._db:
            self._expire(now_ms)
            row = self._db.execute(
                "SELECT id, envelope FROM route_queue WHERE to_ws=? AND done=0 ORDER BY id LIMIT 1", (to_ws,)
            ).fetchone()
        return None if row is None else (row[0], bytes(row[1]))

    def delete(self, qid: int) -> None:
        with self._lock, self._db:
            self._db.execute("UPDATE route_queue SET done=1, envelope=X'' WHERE id=?", (qid,))

    def recent(self, ws_ids: frozenset[str] | set[str], limit: int = 100) -> list[dict]:
        """Hand-offs where one end is in ``ws_ids``, newest first: ids and times only, never bytes. ``waiting`` = the
        recipient has not acknowledged it yet (it is offline, or busy)."""
        ids = sorted(ws_ids)
        if not ids:
            return []
        marks = ",".join("?" * len(ids))
        with self._lock:
            rows = self._db.execute(
                f"SELECT env_id, from_ws, to_ws, enq_ms, done FROM route_queue WHERE from_ws IN ({marks}) OR to_ws IN ({marks}) "
                "ORDER BY id DESC LIMIT ?",
                (*ids, *ids, int(limit)),
            ).fetchall()
        return [
            {"id": e, "from_ws": f, "to_ws": t, "sent_ms": int(ms), "status": "delivered" if d else "waiting"}
            for e, f, t, ms, d in rows
        ]

    def depth(self, to_ws: str) -> int:
        with self._lock:
            return self._db.execute("SELECT COUNT(*) FROM route_queue WHERE to_ws=? AND done=0", (to_ws,)).fetchone()[0]

    def _expire(self, now_ms: int) -> None:
        self._db.execute("DELETE FROM route_queue WHERE enq_ms < ?", (now_ms - self.limits.ttl_ms,))
