"""``.state/index.sqlite``: a derived index of the logs (ticket keys, status, people, labels, members). Never a source
of truth: it is rebuilt from the state when it is missing, unreadable, from another version or stale (its per-log
heads differ from the logs'), and nothing reads it for a decision.

The store keeps it current after every append (only the rows of views that changed). ``dump()`` returns every row in
a canonical order so a test can prove that a rebuild equals the incremental result.
"""

from __future__ import annotations

import contextlib
import json
import sqlite3
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

from .logs import LogInfo

__all__ = ["Index"]

VERSION = "1"
_SCHEMA = """
CREATE TABLE meta(k TEXT PRIMARY KEY, v TEXT NOT NULL);
CREATE TABLE logs(log TEXT PRIMARY KEY, seq INTEGER NOT NULL, head TEXT NOT NULL);
CREATE TABLE tickets(uid TEXT PRIMARY KEY, key TEXT NOT NULL UNIQUE, title TEXT NOT NULL, type TEXT NOT NULL,
  status TEXT NOT NULL, owner TEXT NOT NULL, priority TEXT NOT NULL, size TEXT, due TEXT, parent TEXT,
  waiting INTEGER NOT NULL, frozen INTEGER NOT NULL, seq INTEGER NOT NULL);
CREATE TABLE ticket_people(uid TEXT NOT NULL, role TEXT NOT NULL, person TEXT NOT NULL, PRIMARY KEY(uid, role, person));
CREATE TABLE ticket_labels(uid TEXT NOT NULL, label TEXT NOT NULL, PRIMARY KEY(uid, label));
CREATE TABLE members(person TEXT PRIMARY KEY, name TEXT NOT NULL, role TEXT NOT NULL, ord INTEGER NOT NULL);
CREATE INDEX tickets_status ON tickets(status);
CREATE INDEX people_person ON ticket_people(person);
CREATE INDEX labels_label ON ticket_labels(label);
"""
_TABLES = ("meta", "logs", "tickets", "ticket_people", "ticket_labels", "members")


def _row(view: Any, seq: int) -> tuple[Any, ...]:
    f = view.fields
    return (
        view.uid,
        view.key,
        view.title,
        view.type,
        view.status,
        view.owner,
        f["priority"],
        f["size"],
        f["due"],
        f["parent"],
        int(view.waiting),
        int(view.frozen),
        seq,
    )


class Index:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._db: sqlite3.Connection | None = None
        self._rows: dict[str, Any] = {}

    # -- connection
    def _connect(self) -> sqlite3.Connection:
        if self._db is None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            db = sqlite3.connect(self.path, isolation_level=None, timeout=30)
            db.execute("PRAGMA synchronous=OFF")  # derived data: a crash just means a rebuild
            self._db = db
        return self._db

    def close(self) -> None:
        if self._db is not None:
            self._db.close()
            self._db = None

    def drop(self) -> None:
        """Delete the file (the next append or open rebuilds it)."""
        self._rows = {}
        self.close()
        for suffix in ("", "-journal", "-wal", "-shm"):
            with contextlib.suppress(FileNotFoundError):
                Path(str(self.path) + suffix).unlink()

    # -- freshness
    def is_current(self, logs: Mapping[str, LogInfo], workspace_id: str, genesis: str | None) -> bool:
        if not self.path.exists():
            return False
        try:
            db = self._connect()
            meta = dict(db.execute("SELECT k, v FROM meta"))
            if meta != {"version": VERSION, "workspace_id": workspace_id, "genesis": genesis or ""}:
                return False
            have = {r[0]: (r[1], r[2]) for r in db.execute("SELECT log, seq, head FROM logs")}
            want = {n: (i.seq, i.head) for n, i in logs.items() if i.seq}
            if have != want:
                return False
            self._rows = {}
            return True
        except sqlite3.Error:
            return False

    # -- writing
    def rebuild(self, state: Any, logs: Mapping[str, LogInfo], workspace_id: str, genesis: str | None) -> None:
        self.drop()
        db = self._connect()
        db.executescript("BEGIN;" + _SCHEMA)
        db.executemany(
            "INSERT INTO meta VALUES(?,?)",
            [("version", VERSION), ("workspace_id", workspace_id), ("genesis", genesis or "")],
        )
        self._rows = {}
        self._write(db, state, logs, set(state.tickets), full=True)
        db.execute("COMMIT")

    def update(self, state: Any, logs: Mapping[str, LogInfo], touched_logs: Iterable[str], changed: set[str]) -> None:
        """After an append: refresh the rows of the tickets in ``changed`` (views that are not the old state's), the
        log heads of ``touched_logs`` and, when the workspace log moved, the members."""
        db = self._connect()
        db.execute("BEGIN")
        try:
            self._write(db, state, logs, changed, full=False, touched=set(touched_logs))
            db.execute("COMMIT")
        except BaseException:
            db.execute("ROLLBACK")
            raise

    def _write(
        self,
        db: sqlite3.Connection,
        state: Any,
        logs: Mapping[str, LogInfo],
        uids: set[str],
        *,
        full: bool,
        touched: set[str] | None = None,
    ) -> None:
        for name in sorted(touched if touched is not None else logs):
            i = logs.get(name)
            if i is not None and i.seq:
                db.execute("INSERT OR REPLACE INTO logs VALUES(?,?,?)", (name, i.seq, i.head))
        if full or "workspace" in (touched or ()):
            db.execute("DELETE FROM members")
            db.executemany(
                "INSERT INTO members VALUES(?,?,?,?)",
                [(m.person, m.name, m.role, n) for n, m in enumerate(state.workspace.members.values())],
            )
        for uid in sorted(uids):
            view = state.tickets[uid]
            row = _row(view, logs[uid].seq if uid in logs else 0)
            people = sorted((r, p) for r, ps in view.people.items() for p in ps)
            labels = sorted(view.fields["labels"])
            sig = (row, tuple(people), tuple(labels))
            if self._rows.get(uid) == sig:
                continue
            db.execute("INSERT OR REPLACE INTO tickets VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)", row)
            db.execute("DELETE FROM ticket_people WHERE uid=?", (uid,))
            db.execute("DELETE FROM ticket_labels WHERE uid=?", (uid,))
            db.executemany("INSERT INTO ticket_people VALUES(?,?,?)", [(uid, r, p) for r, p in people])
            db.executemany("INSERT INTO ticket_labels VALUES(?,?)", [(uid, lb) for lb in labels])
            self._rows[uid] = sig

    # -- reading
    def query(self, sql: str, params: Iterable[Any] = ()) -> list[tuple[Any, ...]]:
        return [tuple(r) for r in self._connect().execute(sql, tuple(params))]

    def dump(self) -> dict[str, list[tuple[Any, ...]]]:
        """Every table's rows in a canonical order (for tests and ``orch doctor``)."""
        db = self._connect()
        out = {}
        for t in _TABLES:
            rows = [tuple(r) for r in db.execute(f"SELECT * FROM {t}")]  # noqa: S608 - fixed table names
            out[t] = sorted(rows, key=lambda r: json.dumps(r, default=str))
        return out
