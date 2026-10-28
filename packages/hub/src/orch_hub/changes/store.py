"""SQLite store of access changes that wait for a workspace owner (``<state>/changes.db``, private via ``fsutil``).

Two tables: ``change`` (one proposed grant or membership change, with its state) and ``approval`` (one row per affected
workspace: the owner's yes or no, the snapshot version the proposal was based on, and the digest of the proof that
approved it). ``approval.proof_digest`` is UNIQUE: one proof is never usable twice, whatever it is presented for.
Every failure raises :class:`ChangeError` (fail closed).
"""

from __future__ import annotations

import sqlite3
import threading
from typing import Any

from ..fsutil import PrivateFileError, open_private_db

_SCHEMA = """
PRAGMA foreign_keys = ON;
CREATE TABLE IF NOT EXISTS change (
  id TEXT PRIMARY KEY,
  kind TEXT NOT NULL CHECK (kind IN ('grant','member_add')),
  body TEXT NOT NULL,
  proposer TEXT NOT NULL,
  created_ms INTEGER NOT NULL,
  expires_ms INTEGER NOT NULL,
  state TEXT NOT NULL CHECK (state IN ('pending','applied','rejected','expired','overridden','cancelled')),
  decided_ms INTEGER,
  decided_by TEXT,
  reason TEXT
);
CREATE TABLE IF NOT EXISTS approval (
  change_id TEXT NOT NULL REFERENCES change(id) ON DELETE CASCADE,
  workspace_id TEXT NOT NULL,
  approver_user TEXT NOT NULL,
  base_snapshot_version INTEGER NOT NULL,
  state TEXT NOT NULL CHECK (state IN ('pending','approved','rejected','stale')),
  decided_ms INTEGER,
  proof_digest TEXT UNIQUE,
  PRIMARY KEY (change_id, workspace_id)
);
CREATE INDEX IF NOT EXISTS change_state ON change(state, expires_ms);
"""

CHANGE_COLS = (
    "id",
    "kind",
    "body",
    "proposer",
    "created_ms",
    "expires_ms",
    "state",
    "decided_ms",
    "decided_by",
    "reason",
)
APPROVAL_COLS = (
    "change_id",
    "workspace_id",
    "approver_user",
    "base_snapshot_version",
    "state",
    "decided_ms",
    "proof_digest",
)


class ChangeError(Exception):
    """A refused or failed change operation; ``code`` is stable and goes to the wire, the text never has secrets."""

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code


class ChangesDB:
    def __init__(self, path: str = ":memory:") -> None:
        self.lock = threading.RLock()
        try:
            self._db = open_private_db(str(path), check_same_thread=False, isolation_level=None)
            self._db.executescript(_SCHEMA)
        except PrivateFileError as e:
            raise ChangeError("unavailable", str(e)) from None
        except (sqlite3.Error, OSError) as e:
            raise ChangeError("unavailable", type(e).__name__) from None

    def close(self) -> None:
        with self.lock:
            self._db.close()

    def tx(self) -> _Tx:
        """One immediate transaction under the lock (read-check-write is one step)."""
        return _Tx(self)

    def q(self, sql: str, args: tuple = ()) -> list[tuple]:
        try:
            with self.lock:
                return self._db.execute(sql, args).fetchall()
        except sqlite3.Error as e:
            raise ChangeError("unavailable", type(e).__name__) from None

    def changes(self, where: str = "", args: tuple = (), tail: str = "") -> list[dict[str, Any]]:
        sql = "SELECT " + ", ".join(CHANGE_COLS) + " FROM change" + (" WHERE " + where if where else "")
        return [dict(zip(CHANGE_COLS, r, strict=True)) for r in self.q(sql + " ORDER BY created_ms, id" + tail, args)]

    def change(self, change_id: str) -> dict[str, Any] | None:
        got = self.changes("id=?", (change_id,))
        return got[0] if got else None

    def approvals(self, change_id: str) -> list[dict[str, Any]]:
        rows = self.q(
            "SELECT " + ", ".join(APPROVAL_COLS) + " FROM approval WHERE change_id=? ORDER BY workspace_id",
            (change_id,),
        )
        return [dict(zip(APPROVAL_COLS, r, strict=True)) for r in rows]


class _Tx:
    def __init__(self, d: ChangesDB) -> None:
        self._d = d

    def __enter__(self) -> sqlite3.Connection:
        self._d.lock.acquire()
        try:
            self._d._db.execute("BEGIN IMMEDIATE")
        except sqlite3.Error as e:
            self._d.lock.release()
            raise ChangeError("unavailable", type(e).__name__) from None
        except BaseException:
            self._d.lock.release()
            raise
        return self._d._db

    def __exit__(self, et: object, ev: object, tb: object) -> None:
        try:
            self._d._db.execute("COMMIT" if et is None else "ROLLBACK")
        except sqlite3.Error as e:
            if et is None:
                raise ChangeError("unavailable", type(e).__name__) from None
        finally:
            self._d.lock.release()
