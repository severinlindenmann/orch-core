"""Explicit connections between workspaces (M2, #1414-#1417; Opus design conditions 11-13).

A connection ``A -> B`` means: workspace A may send hand-offs to B. It is directional (``A -> B`` is not ``B -> A``).
Routing is :class:`ConnectionAuthz`: ``may_route(a, b)`` is true only when a connection EXISTS **and** the access model's
own rule (:class:`~orch_hub.router.AccessAuthz`) allows it: access stays the floor, a connection never grants more than
roles allow. Anything unknown or failing is a refusal (fail closed).

The store also keeps ``route_stats``: counts only, per ``(day, from, to)``, never content, ticket ids or people, with a
90-day retention. Who may SEE a count is decided by the caller (the web layer), not here.

``<state>/connections.db`` is a private SQLite file (``fsutil``).
"""

from __future__ import annotations

import sqlite3
import threading
import time
from collections.abc import Callable
from typing import Any

from ..fsutil import PrivateFileError, open_private_db

DAY_MS = 86_400_000
RETENTION_DAYS = 90
TRAFFIC_DAYS = 30

_SCHEMA = """
CREATE TABLE IF NOT EXISTS connection (
  from_ws TEXT NOT NULL, to_ws TEXT NOT NULL, created_ms INTEGER NOT NULL,
  approved_by TEXT, via TEXT NOT NULL CHECK (via IN ('approved','override','migrated')),
  PRIMARY KEY (from_ws, to_ws)
);
CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS route_stats (
  day INTEGER NOT NULL, from_ws TEXT NOT NULL, to_ws TEXT NOT NULL, n INTEGER NOT NULL,
  PRIMARY KEY (day, from_ws, to_ws)
);
"""


class ConnectionError_(Exception):
    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code


class Connections:
    def __init__(self, path: str = ":memory:", *, now_ms: Callable[[], int] | None = None) -> None:
        self._now = now_ms or (lambda: int(time.time() * 1000))
        self.lock = threading.RLock()
        try:
            self._db = open_private_db(str(path), check_same_thread=False)
            self._db.executescript(_SCHEMA)
            self._db.commit()
        except PrivateFileError as e:
            raise ConnectionError_("unavailable", str(e)) from None
        except (sqlite3.Error, OSError) as e:
            raise ConnectionError_("unavailable", type(e).__name__) from None

    def close(self) -> None:
        with self.lock:
            self._db.close()

    # --- the connections themselves ----------------------------------------------------------------------------------

    def allowed(self, a: str, b: str) -> bool:
        """Is there a connection ``a -> b`` right now? Any store error is "no" (fail closed)."""
        try:
            with self.lock:
                return (
                    self._db.execute("SELECT 1 FROM connection WHERE from_ws=? AND to_ws=?", (a, b)).fetchone()
                    is not None
                )
        except sqlite3.Error:
            return False

    def edges(self) -> list[dict[str, Any]]:
        with self.lock:
            rows = self._db.execute(
                "SELECT from_ws, to_ws, created_ms, approved_by, via FROM connection ORDER BY from_ws, to_ws"
            ).fetchall()
        return [{"from": a, "to": b, "created_ms": c, "approved_by": by, "via": via} for a, b, c, by, via in rows]

    def add(self, a: str, b: str, *, approved_by: str | None, via: str) -> bool:
        """Make ``a -> b`` (idempotent). True when it was new."""
        if a == b or via not in ("approved", "override", "migrated"):
            raise ConnectionError_("malformed")
        with self.lock, self._db:
            cur = self._db.execute(
                "INSERT OR IGNORE INTO connection(from_ws, to_ws, created_ms, approved_by, via) VALUES (?,?,?,?,?)",
                (a, b, self._now(), approved_by, via),
            )
        return cur.rowcount == 1

    def remove(self, a: str, b: str) -> bool:
        with self.lock, self._db:
            return self._db.execute("DELETE FROM connection WHERE from_ws=? AND to_ws=?", (a, b)).rowcount == 1

    def forget_workspace(self, ws: str) -> None:
        """A workspace leaves or is removed: no connection to or from it, no counts about it."""
        with self.lock, self._db:
            self._db.execute("DELETE FROM connection WHERE from_ws=? OR to_ws=?", (ws, ws))
            self._db.execute("DELETE FROM route_stats WHERE from_ws=? OR to_ws=?", (ws, ws))

    # --- the one-time backfill --------------------------------------------------------------------------------------------

    def backfill(self, pairs: list[tuple[str, str]]) -> int | None:
        """First start only: make a ``migrated`` connection for every pair routing allowed before this table existed, so an
        upgrade neither breaks nor silently widens routing. Returns how many (``None``: already done). The flag is set in
        the same transaction, so it runs exactly once."""
        with self.lock, self._db:
            if self._db.execute("SELECT 1 FROM meta WHERE k='backfilled'").fetchone():
                return None
            for a, b in pairs:
                self._db.execute(
                    "INSERT OR IGNORE INTO connection(from_ws, to_ws, created_ms, approved_by, via) "
                    "VALUES (?,?,?,NULL,'migrated')",
                    (a, b, self._now()),
                )
            self._db.execute("INSERT INTO meta(k, v) VALUES ('backfilled', '1')")
        return len(pairs)

    # --- counts only ---------------------------------------------------------------------------------------------------

    def bump(self, a: str, b: str) -> None:
        """One hand-off was accepted ``a -> b``. Counts only; never raises into the router; prunes old days."""
        try:
            now = self._now()
            day = now // DAY_MS
            with self.lock, self._db:
                self._db.execute(
                    "INSERT INTO route_stats(day, from_ws, to_ws, n) VALUES (?,?,?,1) "
                    "ON CONFLICT(day, from_ws, to_ws) DO UPDATE SET n = n + 1",
                    (day, a, b),
                )
                self._db.execute("DELETE FROM route_stats WHERE day < ?", (day - RETENTION_DAYS,))
        except sqlite3.Error:
            pass

    def traffic(self, *, days: int = TRAFFIC_DAYS) -> list[dict[str, Any]]:
        """``[{from, to, n}]``: hand-offs per pair over the last ``days`` days (all pairs; the caller scopes them)."""
        since = self._now() // DAY_MS - days
        with self.lock:
            rows = self._db.execute(
                "SELECT from_ws, to_ws, SUM(n) FROM route_stats WHERE day > ? GROUP BY from_ws, to_ws ORDER BY from_ws, to_ws",
                (since,),
            ).fetchall()
        return [{"from": a, "to": b, "n": n} for a, b, n in rows]


class ConnectionAuthz:
    """``may_route`` = a connection exists AND the access rule allows it. The router, the directory and the map use THIS."""

    def __init__(self, connections: Connections, access_authz: Any) -> None:
        self.connections, self.access = connections, access_authz

    def may_route(self, sender_ws: str, recipient_ws: str) -> bool:
        try:
            return bool(self.connections.allowed(sender_ws, recipient_ws)) and bool(
                self.access.may_route(sender_ws, recipient_ws)
            )
        except Exception:  # noqa: BLE001  # fail closed
            return False
