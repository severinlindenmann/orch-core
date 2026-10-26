"""SQLite store of the audit chain (``<state>/audit.db``, private via fsutil, protocol.md §10.5)."""

from __future__ import annotations

import json
import threading
from typing import Any

from ..contract.canon import cj
from ..fsutil import open_private_db

_SCHEMA = """
CREATE TABLE IF NOT EXISTS record (
  seq INTEGER PRIMARY KEY, chain_hash TEXT NOT NULL, ts_ms INTEGER NOT NULL, signed TEXT NOT NULL, event TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS ack (workspace_id TEXT PRIMARY KEY, seq INTEGER NOT NULL);
"""


class AuditStore:
    def __init__(self, path: str = ":memory:") -> None:
        self.lock = threading.RLock()
        self._db = open_private_db(path, check_same_thread=False)
        self._db.executescript(_SCHEMA)
        self._db.commit()

    def close(self) -> None:
        with self.lock:
            self._db.close()

    def head(self) -> dict[str, Any] | None:
        with self.lock:
            r = self._db.execute("SELECT seq, chain_hash, ts_ms FROM record ORDER BY seq DESC LIMIT 1").fetchone()
        return None if r is None else {"seq": r[0], "hash": r[1], "ts_ms": r[2]}

    def append(self, seq: int, chain_hash: str, ts_ms: int, signed: dict[str, Any], event: dict[str, Any]) -> None:
        """Only the next ``seq``: the primary key refuses a second record at a taken one."""
        with self.lock, self._db:
            h = self.head()
            if (h["seq"] if h else 0) + 1 != seq:
                raise ValueError("audit record out of order")
            self._db.execute(
                "INSERT INTO record VALUES (?,?,?,?,?)",
                (seq, chain_hash, ts_ms, cj(signed).decode(), json.dumps(event, sort_keys=True)),
            )

    def after(self, since: int, limit: int) -> list[tuple[int, dict[str, Any], dict[str, Any]]]:
        with self.lock:
            rows = self._db.execute(
                "SELECT seq, signed, event FROM record WHERE seq > ? ORDER BY seq LIMIT ?", (since, limit)
            ).fetchall()
        return [(s, json.loads(sg), json.loads(ev)) for s, sg, ev in rows]

    def hash_at(self, seq: int) -> str | None:
        with self.lock:
            r = self._db.execute("SELECT chain_hash FROM record WHERE seq=?", (seq,)).fetchone()
        return None if r is None else r[0]

    def length(self) -> int:
        h = self.head()
        return 0 if h is None else h["seq"]

    def acked(self, ws: str) -> int:
        with self.lock:
            r = self._db.execute("SELECT seq FROM ack WHERE workspace_id=?", (ws,)).fetchone()
        return 0 if r is None else r[0]

    def put_ack(self, ws: str, seq: int) -> int:
        """Acks only rise; returns the stored value."""
        with self.lock, self._db:
            self._db.execute(
                "INSERT INTO ack VALUES (?,?) ON CONFLICT(workspace_id) DO UPDATE SET seq=MAX(seq, excluded.seq)",
                (ws, seq),
            )
        return self.acked(ws)

    def acks(self) -> dict[str, int]:
        with self.lock:
            return dict(self._db.execute("SELECT workspace_id, seq FROM ack ORDER BY workspace_id").fetchall())
