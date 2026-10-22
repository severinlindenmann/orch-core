"""SQLite store of shared items (``<state>/shares.db``, private via fsutil).

Per (workspace, item) the SEALED bytes exactly as received (never opened here), their SHA-256 and size; plus an
append-only ``share_log`` (change, digest, time; no content) that is the hub-local audit of puts and revokes. No policy
and no clock: :mod:`orch_hub.shares.service` decides, this only stores. One transaction per method.
"""

from __future__ import annotations

import hashlib
import threading
from typing import Any

from ..fsutil import open_private_db

_SCHEMA = """
CREATE TABLE IF NOT EXISTS share_item (
  ws TEXT NOT NULL, item_id TEXT NOT NULL, sealed BLOB NOT NULL, digest TEXT NOT NULL, size INTEGER NOT NULL,
  stored_ms INTEGER NOT NULL, PRIMARY KEY (ws, item_id)
);
CREATE TABLE IF NOT EXISTS share_log (
  id INTEGER PRIMARY KEY AUTOINCREMENT, ws TEXT NOT NULL, item_id TEXT, change TEXT NOT NULL CHECK (change IN
  ('put','replace','revoke','unbind')), digest TEXT, count INTEGER NOT NULL DEFAULT 1, ts_ms INTEGER NOT NULL
);
"""


class ShareStore:
    def __init__(self, path: str = ":memory:") -> None:
        self.lock = threading.RLock()
        self._db = open_private_db(path, check_same_thread=False)
        self._db.executescript(_SCHEMA)
        self._db.commit()

    def close(self) -> None:
        with self.lock:
            self._db.close()

    def get(self, ws: str, item_id: str) -> tuple[bytes, str] | None:
        with self.lock:
            r = self._db.execute("SELECT sealed, digest FROM share_item WHERE ws=? AND item_id=?", (ws, item_id))
            row = r.fetchone()
        return None if row is None else (bytes(row[0]), row[1])

    def usage(self, ws: str) -> tuple[int, int]:
        """``(items, bytes)`` held for the workspace."""
        with self.lock:
            r = self._db.execute("SELECT COUNT(*), COALESCE(SUM(size),0) FROM share_item WHERE ws=?", (ws,)).fetchone()
        return r[0], r[1]

    def put(self, ws: str, item_id: str, sealed: bytes, now_ms: int) -> str:
        """Store or replace; returns ``"put"`` (new) or ``"replace"``. Logs it in the same transaction."""
        digest = hashlib.sha256(sealed).hexdigest()
        with self.lock, self._db:
            held = self._db.execute("SELECT 1 FROM share_item WHERE ws=? AND item_id=?", (ws, item_id)).fetchone()
            self._db.execute(
                "INSERT INTO share_item(ws, item_id, sealed, digest, size, stored_ms) VALUES (?,?,?,?,?,?) "
                "ON CONFLICT(ws, item_id) DO UPDATE SET sealed=excluded.sealed, digest=excluded.digest, "
                "size=excluded.size, stored_ms=excluded.stored_ms",
                (ws, item_id, sealed, digest, len(sealed), now_ms),
            )
            change = "replace" if held else "put"
            self._db.execute(
                "INSERT INTO share_log(ws, item_id, change, digest, ts_ms) VALUES (?,?,?,?,?)",
                (ws, item_id, change, digest, now_ms),
            )
        return change

    def revoke(self, ws: str, item_id: str, now_ms: int) -> bool:
        with self.lock, self._db:
            row = self._db.execute("SELECT digest FROM share_item WHERE ws=? AND item_id=?", (ws, item_id)).fetchone()
            if row is None:
                return False
            self._db.execute("DELETE FROM share_item WHERE ws=? AND item_id=?", (ws, item_id))
            self._db.execute(
                "INSERT INTO share_log(ws, item_id, change, digest, ts_ms) VALUES (?,?,?,?,?)",
                (ws, item_id, "revoke", row[0], now_ms),
            )
        return True

    def purge(self, ws: str, now_ms: int) -> int:
        """Delete every item of the workspace (unbind, switch away from selective)."""
        with self.lock, self._db:
            n = self._db.execute("SELECT COUNT(*) FROM share_item WHERE ws=?", (ws,)).fetchone()[0]
            if n:
                self._db.execute("DELETE FROM share_item WHERE ws=?", (ws,))
                self._db.execute(
                    "INSERT INTO share_log(ws, item_id, change, count, ts_ms) VALUES (?,?,?,?,?)",
                    (ws, None, "unbind", n, now_ms),
                )
        return n

    def items(self, ws: str) -> list[dict[str, Any]]:
        with self.lock:
            rows = self._db.execute(
                "SELECT item_id, sealed, digest, size, stored_ms FROM share_item WHERE ws=? ORDER BY item_id", (ws,)
            ).fetchall()
        return [{"item_id": i, "sealed": bytes(s), "digest": d, "size": z, "stored_ms": t} for i, s, d, z, t in rows]

    def workspaces(self) -> list[str]:
        with self.lock:
            return [r[0] for r in self._db.execute("SELECT DISTINCT ws FROM share_item ORDER BY ws").fetchall()]

    def log(self, ws: str | None = None) -> list[dict[str, Any]]:
        q, a = "SELECT ws, item_id, change, digest, count, ts_ms FROM share_log", ()
        if ws is not None:
            q, a = q + " WHERE ws=?", (ws,)
        with self.lock:
            rows = self._db.execute(q + " ORDER BY id", a).fetchall()
        keys = ("ws", "item_id", "change", "digest", "count", "ts_ms")
        return [dict(zip(keys, r, strict=True)) for r in rows]
