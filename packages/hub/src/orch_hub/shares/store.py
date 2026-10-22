"""SQLite store of shared items (``<state>/shares.db``, private via fsutil).

Per (workspace, item) the SEALED bytes exactly as received (never opened here), their SHA-256 and size, the
``item_version`` (from the sealed prefix, checked by the service) and the signed item's ``item_hash``. A revoked item
(or one purged by an unbind) stays as a TOMBSTONE row ``{item_id, item_version}`` with ``revoked=1`` and no sealed bytes
(protocol.md §10.2b): a revoked version never comes back. Plus an append-only ``share_log`` (change, digest, time; no
content), the hub-local audit next to the chain's ``share`` events. No policy and no clock: :mod:`orch_hub.shares.service`
decides, this only stores. One transaction per method.
"""

from __future__ import annotations

import hashlib
import threading
from typing import Any

from ..fsutil import open_private_db

_SCHEMA = """
CREATE TABLE IF NOT EXISTS share_item (
  ws TEXT NOT NULL, item_id TEXT NOT NULL, sealed BLOB NOT NULL, digest TEXT NOT NULL, size INTEGER NOT NULL,
  stored_ms INTEGER NOT NULL, item_version INTEGER NOT NULL, item_hash TEXT NOT NULL,
  revoked INTEGER NOT NULL DEFAULT 0, PRIMARY KEY (ws, item_id)
);
CREATE TABLE IF NOT EXISTS share_pending (ws TEXT PRIMARY KEY);
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
        """``(sealed, digest)`` of a LIVE item (None for an unknown or revoked one)."""
        with self.lock:
            r = self._db.execute(
                "SELECT sealed, digest FROM share_item WHERE ws=? AND item_id=? AND revoked=0", (ws, item_id)
            )
            row = r.fetchone()
        return None if row is None else (bytes(row[0]), row[1])

    def held(self, ws: str, item_id: str) -> dict[str, Any] | None:
        """What the hub holds under ``item_id``, tombstones included: ``{item_version, hash, revoked}`` (the shape
        ``phase4.open_share_item`` takes)."""
        with self.lock:
            row = self._db.execute(
                "SELECT item_version, item_hash, revoked FROM share_item WHERE ws=? AND item_id=?", (ws, item_id)
            ).fetchone()
        return None if row is None else {"item_version": row[0], "hash": row[1], "revoked": bool(row[2])}

    def usage(self, ws: str) -> tuple[int, int]:
        """``(live items, sealed bytes)`` held for the workspace (tombstones are neither)."""
        with self.lock:
            r = self._db.execute(
                "SELECT COUNT(*), COALESCE(SUM(size),0) FROM share_item WHERE ws=? AND revoked=0", (ws,)
            ).fetchone()
        return r[0], r[1]

    def put(self, ws: str, item_id: str, sealed: bytes, item_version: int, item_hash: str, now_ms: int) -> str:
        """Store or replace (also over a tombstone); returns ``"put"`` (new live item) or ``"replace"``. Logs it in
        the same transaction."""
        digest = hashlib.sha256(sealed).hexdigest()
        with self.lock, self._db:
            held = self._db.execute(
                "SELECT 1 FROM share_item WHERE ws=? AND item_id=? AND revoked=0", (ws, item_id)
            ).fetchone()
            self._db.execute(
                "INSERT INTO share_item(ws, item_id, sealed, digest, size, stored_ms, item_version, item_hash, revoked) "
                "VALUES (?,?,?,?,?,?,?,?,0) ON CONFLICT(ws, item_id) DO UPDATE SET sealed=excluded.sealed, "
                "digest=excluded.digest, size=excluded.size, stored_ms=excluded.stored_ms, "
                "item_version=excluded.item_version, item_hash=excluded.item_hash, revoked=0",
                (ws, item_id, sealed, digest, len(sealed), now_ms, item_version, item_hash),
            )
            change = "replace" if held else "put"
            self._db.execute(
                "INSERT INTO share_log(ws, item_id, change, digest, ts_ms) VALUES (?,?,?,?,?)",
                (ws, item_id, change, digest, now_ms),
            )
        return change

    def revoke(self, ws: str, item_id: str, now_ms: int) -> bool:
        """Turn a live item into a tombstone (sealed bytes dropped, version kept); False if nothing live was held."""
        with self.lock, self._db:
            row = self._db.execute(
                "SELECT digest FROM share_item WHERE ws=? AND item_id=? AND revoked=0", (ws, item_id)
            ).fetchone()
            if row is None:
                return False
            self._tombstone(ws, item_id)
            self._db.execute(
                "INSERT INTO share_log(ws, item_id, change, digest, ts_ms) VALUES (?,?,?,?,?)",
                (ws, item_id, "revoke", row[0], now_ms),
            )
        return True

    def _tombstone(self, ws: str, item_id: str | None) -> None:
        q = "UPDATE share_item SET revoked=1, sealed=x'', size=0 WHERE ws=? AND revoked=0"
        if item_id is None:
            self._db.execute(q, (ws,))
        else:
            self._db.execute(q + " AND item_id=?", (ws, item_id))

    def purge(self, ws: str, now_ms: int) -> int:
        """Every live item of the workspace goes (unbind, switch away from selective); the versions stay as
        tombstones, so a stale put cannot replay an old item after a rebind."""
        with self.lock, self._db:
            n = self._db.execute("SELECT COUNT(*) FROM share_item WHERE ws=? AND revoked=0", (ws,)).fetchone()[0]
            if n:
                self._tombstone(ws, None)
                self._db.execute(
                    "INSERT INTO share_log(ws, item_id, change, count, ts_ms) VALUES (?,?,?,?,?)",
                    (ws, None, "unbind", n, now_ms),
                )
        return n

    def mark_pending(self, ws: str) -> None:
        """A purge of ``ws`` failed: its items belong to a binding that is gone, until a purge succeeds (durable)."""
        with self.lock, self._db:
            self._db.execute("INSERT OR IGNORE INTO share_pending(ws) VALUES (?)", (ws,))

    def clear_pending(self, ws: str) -> None:
        with self.lock, self._db:
            self._db.execute("DELETE FROM share_pending WHERE ws=?", (ws,))

    def pending(self) -> set[str]:
        with self.lock:
            return {r[0] for r in self._db.execute("SELECT ws FROM share_pending").fetchall()}

    def items(self, ws: str) -> list[dict[str, Any]]:
        with self.lock:
            rows = self._db.execute(
                "SELECT item_id, sealed, digest, size, stored_ms, item_version FROM share_item "
                "WHERE ws=? AND revoked=0 ORDER BY item_id",
                (ws,),
            ).fetchall()
        return [
            {"item_id": i, "sealed": bytes(s), "digest": d, "size": z, "stored_ms": t, "item_version": v}
            for i, s, d, z, t, v in rows
        ]

    def workspaces(self) -> list[str]:
        with self.lock:
            return [
                r[0]
                for r in self._db.execute("SELECT DISTINCT ws FROM share_item WHERE revoked=0 ORDER BY ws").fetchall()
            ]

    def log(self, ws: str | None = None) -> list[dict[str, Any]]:
        q, a = "SELECT ws, item_id, change, digest, count, ts_ms FROM share_log", ()
        if ws is not None:
            q, a = q + " WHERE ws=?", (ws,)
        with self.lock:
            rows = self._db.execute(q + " ORDER BY id", a).fetchall()
        keys = ("ws", "item_id", "change", "digest", "count", "ts_ms")
        return [dict(zip(keys, r, strict=True)) for r in rows]
