"""``.state/index.sqlite``: a derived index of the logs (ticket keys, status, people, labels, members). Never a source
of truth: it is rebuilt from the state when it is missing, unreadable, from another version or stale (its per-log
heads differ from the logs'), and nothing reads it for a decision.

The store keeps it current after every append (only the rows of views that changed). ``dump()`` returns every row in
a canonical order so a test can prove that a rebuild equals the incremental result.
"""

from __future__ import annotations

import contextlib
import json
import os
import sqlite3
import stat
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Any

from . import fsio
from .errors import StoreError
from .logs import LogInfo

if TYPE_CHECKING:
    from .rootfs import RootFS

__all__ = ["Index"]

VERSION = "2"
_SCHEMA = """
CREATE TABLE meta(k TEXT PRIMARY KEY, v TEXT NOT NULL);
CREATE TABLE logs(log TEXT PRIMARY KEY, seq INTEGER NOT NULL, head TEXT NOT NULL, size INTEGER NOT NULL);
CREATE TABLE tickets(uid TEXT PRIMARY KEY, key TEXT NOT NULL UNIQUE, title TEXT NOT NULL, type TEXT NOT NULL,
  status TEXT NOT NULL, owner TEXT NOT NULL, priority TEXT NOT NULL, size TEXT, due TEXT, parent TEXT,
  waiting INTEGER NOT NULL, frozen INTEGER NOT NULL, seq INTEGER NOT NULL, visibility TEXT NOT NULL);
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
        json.dumps(
            "workspace" if view.visibility == "workspace" else {"restricted": list(view.visibility["restricted"])}
        ),
    )


#: an index file larger than this is not loaded (it is rebuilt): derived data, and loading it costs memory
MAX_INDEX_BYTES = 256 << 20
_LEGACY = ("-journal", "-wal", "-shm")  # what a file-backed SQLite of an older orch may have left next to it


class StaleIndex(Exception):
    """The file changed since this object loaded or saved it (another :class:`~orch.store.Store` saved it): this
    in-memory copy is closed and must not be saved over the other's rows. The store re-checks (reloads or rebuilds)."""


class Index:
    """The derived index (#564): an **in-memory** SQLite database. It is loaded from ``.state/index.sqlite`` with
    :meth:`RootFS.read_or_none` + ``deserialize`` and saved with ``serialize`` + :meth:`RootFS.write_atomic` (temp
    file, rename), both through the store's pinned root: SQLite never opens a file or a journal under the workspace,
    so no symlink, swapped ``.state`` or reconnect can send a write elsewhere. Semantics as before: derived data,
    checked against the logs' sizes, rebuilt when missing, stale or corrupt. Saved after every rebuild and every
    update (not ``fsync``-ed: a crash only means a rebuild); another process's save is noticed by the file's
    signature and reloaded at the next :meth:`is_current`."""

    def __init__(self, path: Path, fs: RootFS | None = None) -> None:
        self.path = path
        self._fs = fs
        self._db: sqlite3.Connection | None = None
        self._rows: dict[str, Any] = {}
        self._sig: tuple[int, int, int] | None = None  # the file as loaded or last saved by this object

    # -- the file, below the pinned root
    def _file_sig(self) -> tuple[int, int, int] | None:
        try:
            st = self._fs.lstat(self.path) if self._fs is not None else os.lstat(self.path)
        except (OSError, StoreError):
            return None
        return (st.st_ino, st.st_size, st.st_mtime_ns) if stat.S_ISREG(st.st_mode) else None

    def _read(self) -> bytes | None:
        try:
            raw = (
                self._fs.read_or_none(self.path, MAX_INDEX_BYTES)
                if self._fs is not None
                else fsio.read_or_none(self.path, MAX_INDEX_BYTES)
            )
        except (OSError, StoreError):
            return None
        return raw if raw is not None and len(raw) <= MAX_INDEX_BYTES else None

    def _save(self) -> None:
        assert self._db is not None
        data = self._db.serialize()
        if self._fs is not None:
            self._fs.write_atomic(self.path, data, durable=False)
        else:
            fsio.write_atomic(self.path, data, durable=False)
        self._sig = self._file_sig()

    def stale(self) -> bool:
        """Is the file not the one this object loaded or last saved (one ``lstat``, below the pinned root)? A closed
        index counts as stale: it is reloaded and checked before it is trusted again."""
        return self._db is None or self._file_sig() != self._sig

    # -- connection
    def _connect(self) -> sqlite3.Connection:
        if self._db is None:
            db = sqlite3.connect(":memory:", isolation_level=None)
            raw = self._read()
            if raw:
                try:
                    db.deserialize(raw)
                except sqlite3.Error:  # not a database: an empty one, rebuilt by the caller
                    db.close()
                    db = sqlite3.connect(":memory:", isolation_level=None)
            self._sig = self._file_sig()
            self._rows = {}
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
        self._sig = None
        for suffix in ("", *_LEGACY):
            target = self.path.with_name(self.path.name + suffix)
            with contextlib.suppress(FileNotFoundError):
                if self._fs is not None:
                    self._fs.unlink(target)
                else:
                    os.unlink(target)

    # -- freshness
    def is_current(self, sizes: Mapping[str, int], workspace_id: str, genesis: str | None) -> bool:
        """The index describes these logs: same version, workspace and genesis, and every log has the size the index
        recorded (a log that grew, shrank or appeared without the index being told is stale)."""
        sig = self._file_sig()
        if sig is None:
            return False
        if self._db is not None and sig != self._sig:
            self.close()  # another process saved it: load what is on disk now
        try:
            db = self._connect()
            meta = dict(db.execute("SELECT k, v FROM meta"))
            if meta != {"version": VERSION, "workspace_id": workspace_id, "genesis": genesis or ""}:
                return False
            have = {r[0]: r[1] for r in db.execute("SELECT log, size FROM logs")}
            if have != {n: sz for n, sz in sizes.items() if sz}:
                return False
            self._rows = {}
            return True
        except sqlite3.Error:
            return False

    # -- writing
    def rebuild(
        self, state: Any, logs: Mapping[str, LogInfo], workspace_id: str, genesis: str | None, sizes: Mapping[str, int]
    ) -> None:
        self.drop()
        db = self._connect()
        db.executescript("BEGIN;" + _SCHEMA)
        db.executemany(
            "INSERT INTO meta VALUES(?,?)",
            [("version", VERSION), ("workspace_id", workspace_id), ("genesis", genesis or "")],
        )
        self._rows = {}
        self._write(db, state, logs, set(state.tickets), full=True, sizes=sizes)
        db.execute("COMMIT")
        self._save()

    def update(
        self,
        state: Any,
        logs: Mapping[str, LogInfo],
        touched_logs: Iterable[str],
        changed: set[str],
        sizes: Mapping[str, int],
    ) -> None:
        """After an append: refresh the rows of the tickets in ``changed`` (views that are not the old state's), the
        log heads of ``touched_logs`` and, when the workspace log moved, the members. Refuses (:class:`StaleIndex`,
        closed) when another store saved the file since this one loaded it: its rows are never overwritten."""
        if self.stale():
            self.close()
            raise StaleIndex()
        db = self._connect()
        db.execute("BEGIN")
        try:
            self._write(db, state, logs, changed, full=False, touched=set(touched_logs), sizes=sizes)
            db.execute("COMMIT")
        except BaseException:
            db.execute("ROLLBACK")
            raise
        self._save()

    def _write(
        self,
        db: sqlite3.Connection,
        state: Any,
        logs: Mapping[str, LogInfo],
        uids: set[str],
        *,
        full: bool,
        touched: set[str] | None = None,
        sizes: Mapping[str, int],
    ) -> None:
        for name in sorted(touched if touched is not None else logs):
            i = logs.get(name)
            if i is not None and i.seq:
                db.execute("INSERT OR REPLACE INTO logs VALUES(?,?,?,?)", (name, i.seq, i.head, sizes[name]))
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
            db.execute("INSERT OR REPLACE INTO tickets VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)", row)
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
