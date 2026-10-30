"""The webhook inbox store (SQLite, 0600): one row per hook, at most one ring row per hook.

A ring row is ``(seq, token_hash, workspace, source, received_ms)``: no body, no header, no follow-up id. Every read and
write that a workspace asks for is keyed by the workspace of its authenticated link. Expiry is lazy and indexed
(``expires_ms``), at most ``PURGE`` rows per call, so no call scans a table."""

from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Any

from ..contract import hooks as HK
from ..contract.hub import Refused
from ..fsutil import open_private_db

PURGE = 100
_SCHEMA = """
CREATE TABLE IF NOT EXISTS hook (
  token_hash TEXT PRIMARY KEY, ws TEXT NOT NULL, followup TEXT NOT NULL, source TEXT NOT NULL,
  secret TEXT NOT NULL, expires_ms INTEGER NOT NULL, rung_ms INTEGER
);
CREATE INDEX IF NOT EXISTS hook_ws ON hook (ws);
CREATE INDEX IF NOT EXISTS hook_exp ON hook (expires_ms);
CREATE TABLE IF NOT EXISTS ring (
  seq INTEGER PRIMARY KEY AUTOINCREMENT, token_hash TEXT NOT NULL UNIQUE, ws TEXT NOT NULL, source TEXT NOT NULL,
  received_ms INTEGER NOT NULL, expires_ms INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS ring_ws ON ring (ws, seq);
CREATE INDEX IF NOT EXISTS ring_exp ON ring (expires_ms);
CREATE TABLE IF NOT EXISTS ws_head (ws TEXT PRIMARY KEY, head INTEGER NOT NULL);
"""


@dataclass(frozen=True)
class Hook:
    token_hash: str
    ws: str
    source: str
    secret: str
    expires_ms: int


class HookStore:
    def __init__(self, path: str = ":memory:") -> None:
        self._lock = threading.Lock()
        self._db = open_private_db(path, check_same_thread=False)
        self._db.executescript(_SCHEMA)
        self._db.commit()

    def close(self) -> None:
        with self._lock:
            self._db.close()

    def _purge(self, now_ms: int) -> None:
        self._db.execute(
            "DELETE FROM hook WHERE token_hash IN (SELECT token_hash FROM hook WHERE expires_ms <= ? LIMIT ?)",
            (now_ms, PURGE),
        )
        self._db.execute(
            "DELETE FROM ring WHERE seq IN (SELECT seq FROM ring WHERE expires_ms <= ? LIMIT ?)", (now_ms, PURGE)
        )

    # --- link ops (workspace scoped) -------------------------------------------------------------------------------

    def put(self, ws: str, body: dict[str, Any], now_ms: int) -> None:
        th, exp = body["token_hash"], body["expires_ms"]
        if exp <= now_ms or exp > now_ms + HK.MAX_EXPIRY_MS:
            raise Refused("bad_expiry")
        replaces = body.get("replaces")
        if replaces == th:
            raise Refused("malformed")
        with self._lock, self._db:
            self._purge(now_ms)
            if self._db.execute("SELECT 1 FROM hook WHERE token_hash = ?", (th,)).fetchone():
                raise Refused("hook_exists")
            if replaces is not None:  # only the caller's own hook; another workspace's hash is silently not touched
                self._drop(ws, replaces)
            n = self._db.execute("SELECT COUNT(*) FROM hook WHERE ws = ?", (ws,)).fetchone()[0]
            if n >= HK.MAX_HOOKS_PER_WORKSPACE:
                raise Refused("too_many_hooks")
            self._db.execute(
                "INSERT INTO hook (token_hash, ws, followup, source, secret, expires_ms) VALUES (?,?,?,?,?,?)",
                (th, ws, body["followup"], body["source"], body["secret"], exp),
            )

    def _drop(self, ws: str, th: str) -> None:
        self._db.execute("DELETE FROM ring WHERE token_hash = ? AND ws = ?", (th, ws))
        self._db.execute("DELETE FROM hook WHERE token_hash = ? AND ws = ?", (th, ws))

    def revoke(self, ws: str, th: str) -> None:
        with self._lock, self._db:
            self._drop(ws, th)

    def pull(self, ws: str, since: int, now_ms: int) -> dict[str, Any]:
        with self._lock, self._db:
            self._purge(now_ms)
            rows = self._db.execute(
                "SELECT seq, token_hash, source, received_ms FROM ring WHERE ws = ? AND seq > ? ORDER BY seq LIMIT ?",
                (ws, since, HK.PULL_MAX),
            ).fetchall()
            head = self._db.execute("SELECT head FROM ws_head WHERE ws = ?", (ws,)).fetchone()
        return {
            "rings": [
                {"seq": s, "token_hash": t, "source": src, "kind": HK.KIND, "received_at": ms} for s, t, src, ms in rows
            ],
            "head": head[0] if head else 0,
        }

    def ack(self, ws: str, seq: int) -> int:
        with self._lock, self._db:
            self._db.execute("DELETE FROM ring WHERE ws = ? AND seq <= ?", (ws, seq))
        return seq

    # --- the ring (the public path) --------------------------------------------------------------------------------

    def lookup(self, token_hash: str, now_ms: int) -> Hook | None:
        with self._lock:
            r = self._db.execute(
                "SELECT ws, source, secret, expires_ms FROM hook WHERE token_hash = ?", (token_hash,)
            ).fetchone()
        if r is None or r[3] <= now_ms:
            return None
        return Hook(token_hash, r[0], r[1], r[2], r[3])

    def ring(self, hook: Hook, now_ms: int) -> bool:
        """Store the first ring of a hook. False when it had rung already (or the hook is gone): nothing changes."""
        with self._lock, self._db:
            # the flag is on the hook, so a ring that was acked (its row deleted) cannot ring again
            cur = self._db.execute(
                "UPDATE hook SET rung_ms = ? WHERE token_hash = ? AND rung_ms IS NULL", (now_ms, hook.token_hash)
            )
            if cur.rowcount != 1:
                return False
            cur = self._db.execute(
                "INSERT INTO ring (token_hash, ws, source, received_ms, expires_ms) VALUES (?,?,?,?,?)",
                (hook.token_hash, hook.ws, hook.source, now_ms, hook.expires_ms),
            )
            self._db.execute(
                "INSERT INTO ws_head (ws, head) VALUES (?, ?) ON CONFLICT(ws) DO UPDATE SET head = excluded.head",
                (hook.ws, cur.lastrowid),
            )
            return True

    def counts(self, ws: str) -> tuple[int, int]:
        """(hooks, rings) held for a workspace; for tests and the doctor."""
        with self._lock:
            h = self._db.execute("SELECT COUNT(*) FROM hook WHERE ws = ?", (ws,)).fetchone()[0]
            r = self._db.execute("SELECT COUNT(*) FROM ring WHERE ws = ?", (ws,)).fetchone()[0]
        return h, r
