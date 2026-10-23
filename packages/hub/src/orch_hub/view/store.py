"""The hub's durable per-workspace view store (protocol.md §10.7).

What it keeps: for every ``(workspace, log)`` the event HEADS the hub accepted (index ``i`` = ``seq`` ``i + 1``), and
the ``log_sync`` pages that carried the lines, STILL SEALED under their ``K_view(epoch)`` (the AEAD ciphertext as the
host sent it). The hub's database alone therefore reads nothing; lines are opened only by :mod:`.build` / :mod:`.needs`
with a ``WK_e`` that lives in memory for that one call. State is in its own private SQLite file
(``fsutil.open_private_db``: created 0600, refused if anything else).

``commit`` is one transaction per page; a reset of a log drops its heads and pages first, and a reset of the
workspace log also drops EVERY ticket view (L2P4-9).
"""

from __future__ import annotations

import json
import sqlite3
import threading
from typing import Any

from ..contract.hub import Refused
from ..fsutil import open_private_db

MAX_WS_BYTES = 64 << 20  # sealed page bytes kept per workspace (a guard; the host resends from seq 1 with `reset`)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS view_heads (
  ws TEXT NOT NULL, log TEXT NOT NULL, seq INTEGER NOT NULL, head TEXT NOT NULL, PRIMARY KEY (ws, log, seq)
);
CREATE TABLE IF NOT EXISTS view_pages (
  id INTEGER PRIMARY KEY AUTOINCREMENT, ws TEXT NOT NULL, log TEXT NOT NULL, epoch INTEGER NOT NULL,
  from_seq INTEGER NOT NULL, count INTEGER NOT NULL, body TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS view_proj (
  ws TEXT NOT NULL, log TEXT NOT NULL, epoch INTEGER NOT NULL, seq INTEGER NOT NULL, ws_seq INTEGER NOT NULL,
  body TEXT NOT NULL, PRIMARY KEY (ws, log)
);
CREATE INDEX IF NOT EXISTS view_pages_log ON view_pages (ws, log, id);
"""


class ViewStore:
    def __init__(self, path: str) -> None:
        self.lock = threading.RLock()
        self._db = open_private_db(path, check_same_thread=False)
        self._db.executescript(_SCHEMA)
        self._db.commit()

    def close(self) -> None:
        with self.lock:
            self._db.close()

    def heads(self, ws: str, log: str) -> list[str]:
        with self.lock:
            rows = self._db.execute("SELECT head FROM view_heads WHERE ws=? AND log=? ORDER BY seq", (ws, log))
            return [r[0] for r in rows.fetchall()]

    def workspaces(self) -> list[str]:
        with self.lock:
            return [r[0] for r in self._db.execute("SELECT DISTINCT ws FROM view_heads ORDER BY ws")]

    def logs(self, ws: str) -> list[str]:
        with self.lock:
            rows = self._db.execute("SELECT DISTINCT log FROM view_heads WHERE ws=? ORDER BY log", (ws,))
            return [r[0] for r in rows.fetchall()]

    def pages(self, ws: str, log: str) -> list[dict[str, Any]]:
        """The stored (sealed) bodies of one log, oldest first."""
        with self.lock:
            rows = self._db.execute("SELECT body FROM view_pages WHERE ws=? AND log=? ORDER BY id", (ws, log))
            return [json.loads(r[0]) for r in rows.fetchall()]

    def proj_key(self, ws: str, log: str) -> tuple[int, int, int] | None:
        """``(epoch, seq, ws_seq)`` of the projection held for ``log`` (D15b), or None."""
        with self.lock:
            r = self._db.execute("SELECT epoch, seq, ws_seq FROM view_proj WHERE ws=? AND log=?", (ws, log)).fetchone()
            return None if r is None else (r[0], r[1], r[2])

    def proj_page(self, ws: str, log: str) -> dict[str, Any] | None:
        """The sealed page body that carried the held projection of ``log`` (its ``proj`` stays sealed at rest)."""
        with self.lock:
            r = self._db.execute("SELECT body FROM view_proj WHERE ws=? AND log=?", (ws, log)).fetchone()
            return None if r is None else json.loads(r[0])

    def commit(
        self,
        ws: str,
        log: str,
        out: list[str],
        held: int,
        body: dict[str, Any],
        *,
        reset: bool,
        proj: tuple[int, int] | None = None,
    ) -> None:
        """Replace/extend the heads of ``log`` with ``out`` (the view after the page; ``held`` = how many the store had
        before, 0 with ``reset``) and keep the sealed page when it added anything. ``proj`` = ``(seq, ws_seq)`` when
        this page's projection replaces the held one (the whole sealed page is kept for it). All in ONE transaction."""
        raw = json.dumps(body, sort_keys=True, separators=(",", ":"))
        try:
            with self.lock, self._db:
                if reset:
                    self._db.execute("DELETE FROM view_heads WHERE ws=? AND log=?", (ws, log))
                    self._db.execute("DELETE FROM view_pages WHERE ws=? AND log=?", (ws, log))
                    self._db.execute("DELETE FROM view_proj WHERE ws=? AND log=?", (ws, log))
                    if log == "workspace":  # L2P4-9: every ticket view goes with it
                        self._db.execute("DELETE FROM view_heads WHERE ws=?", (ws,))
                        self._db.execute("DELETE FROM view_pages WHERE ws=?", (ws,))
                        self._db.execute("DELETE FROM view_proj WHERE ws=?", (ws,))
                    held = 0
                if len(out) > held:
                    used = self._db.execute("SELECT COALESCE(SUM(LENGTH(body)),0) FROM view_pages WHERE ws=?", (ws,))
                    if used.fetchone()[0] + len(raw) > MAX_WS_BYTES:
                        raise Refused("too_large")
                    self._db.executemany(
                        "INSERT INTO view_heads(ws, log, seq, head) VALUES (?,?,?,?)",
                        [(ws, log, i + 1, out[i]) for i in range(held, len(out))],
                    )
                    self._db.execute(
                        "INSERT INTO view_pages(ws, log, epoch, from_seq, count, body) VALUES (?,?,?,?,?,?)",
                        (ws, log, body["epoch"], body["from_seq"], body["count"], raw),
                    )
                if proj is not None:
                    self._db.execute(
                        "INSERT OR REPLACE INTO view_proj(ws, log, epoch, seq, ws_seq, body) VALUES (?,?,?,?,?,?)",
                        (ws, log, body["epoch"], proj[0], proj[1], raw),
                    )
        except sqlite3.Error:
            raise Refused("internal") from None

    def drop(self, ws: str) -> None:
        """The workspace left this hub (or is no longer full): forget its view."""
        try:
            with self.lock, self._db:
                self._db.execute("DELETE FROM view_heads WHERE ws=?", (ws,))
                self._db.execute("DELETE FROM view_pages WHERE ws=?", (ws,))
                self._db.execute("DELETE FROM view_proj WHERE ws=?", (ws,))
        except sqlite3.Error:
            raise Refused("internal") from None
