"""The rid store of R2 (contract 11A.3 ``rids.sqlite``, 11A.4 "The rid state machine"): the durable exactly-once record.

Tables::

    rid(device, rid, first_ms, req_digest, epoch, seq, state, principal, idem, kind)
                                                   state: admitted|answered|tombstone
    submission(device, rid, op, log, events)       the device-signed events of an admitted write (11A.3 exception)
    chunk(device, rid, idx, epoch, bytes, state)   state: stored|attempted|posted|expired|exhausted|conflict|dropped
    window(device, high, bitmap)                   the seq window (64), never dropped with the rids
    cursor(after)                                  the poll cursor: end of the contiguous durable prefix
    meta(k, v)                                     start_ms (the start fence)

``kind``: ``read``/``write`` (admitted to run), ``refusal`` (refused after step 5, answered at entry), ``unverified``
(a step-4 refusal: device, signature; recorded so a resend replays the same bytes, never consumes a seq).

``device`` is the verified device id, except for ``unverified`` records: those are keyed ``u:<header device>`` (R2-11),
so a request a verified device sends never matches a refusal recorded for a forged header naming it.

**Growth bound** (R2-3/R2-4; the caps are the bridge's, checked before every insert): rows per device per 15 min
``PER_DEVICE + BUSY_ALLOWANCE`` (1 088) and per 24 h ``DEVICE_DAY_ROWS`` (8 192); host-wide per 15 min ``MAX_RECORDS``
(16 384) and per 24 h ``MAX_RETAINED_ROWS`` (131 072); tombstones at most :data:`MAX_TOMBSTONES` (the oldest go first;
the seq window and the ±300 s check keep refusing an old request without them); stored chunk bytes per device and
host-wide within the bridge's byte budgets. ``auto_vacuum=INCREMENTAL``: every sweep returns free pages to the file
system (:data:`VACUUM_PAGES` at a time).

Every transition is ONE SQLite transaction (``synchronous=FULL``, WAL): T1 :meth:`admit` (window + rid + submission),
T2 :meth:`answer` (chunks ``stored`` + state ``answered`` + submission deleted), the refusals (:meth:`record_refusal`:
rid ``answered`` + its chunk, and the window when the step consumes the seq), and the chunk marks. Nothing is ever
POSTed before the transaction that stores its bytes has committed; an ``attempted`` chunk's bytes are frozen.

**Opening** follows 11A.3: the checked open of the database and both sidecars relative to the locked state directory
(``O_NOFOLLOW``, 0600, owner, ``nlink == 1``; the fds stay open), ``sqlite3.connect("file:...?mode=rw")``, then a
``stat`` of every path compared with the held fds (``(st_dev, st_ino)``); a mismatch refuses (``relay_state_unsafe``).
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from typing import Any

from .state import StateDir, StateError

__all__ = [
    "CHUNK_KEEP_MS",
    "MAX_TOMBSTONES",
    "RID_KEEP_MS",
    "SEQ_WINDOW",
    "TOMBSTONE_KEEP_MS",
    "Chunk",
    "Rid",
    "RidStore",
    "seq_accept",
]

DB = "rids.sqlite"
SEQ_WINDOW = 64
RID_KEEP_MS = 24 * 3600 * 1000  # a rid record lives 24 h after first_ms (then a tombstone)
TOMBSTONE_KEEP_MS = 7 * 24 * 3600 * 1000  # a tombstone lives 7 days
CHUNK_KEEP_MS = 960_000  # chunk bytes are dropped at first_ms + 960 s (the relay refuses re-stores after 900 s)
MAX_TOMBSTONES = 262_144  # the oldest tombstones go first beyond this (about 65 MB at about 250 B each)
VACUUM_PAGES = 4096  # pages returned per sweep (auto_vacuum=INCREMENTAL)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS rid(device TEXT NOT NULL, rid TEXT NOT NULL, first_ms INTEGER NOT NULL,
  req_digest TEXT NOT NULL, epoch INTEGER NOT NULL, seq INTEGER NOT NULL, state TEXT NOT NULL, principal TEXT,
  idem TEXT, kind TEXT NOT NULL, PRIMARY KEY(device, rid));
CREATE INDEX IF NOT EXISTS rid_idem ON rid(device, idem);
CREATE INDEX IF NOT EXISTS rid_dev_first ON rid(device, first_ms);
CREATE INDEX IF NOT EXISTS rid_first ON rid(first_ms);
CREATE TABLE IF NOT EXISTS submission(device TEXT NOT NULL, rid TEXT NOT NULL, op TEXT NOT NULL, log TEXT NOT NULL,
  events TEXT NOT NULL, PRIMARY KEY(device, rid));
CREATE TABLE IF NOT EXISTS chunk(device TEXT NOT NULL, rid TEXT NOT NULL, idx INTEGER NOT NULL, epoch INTEGER NOT NULL,
  bytes BLOB, state TEXT NOT NULL, PRIMARY KEY(device, rid, idx));
CREATE INDEX IF NOT EXISTS chunk_live ON chunk(device) WHERE bytes IS NOT NULL;
CREATE TABLE IF NOT EXISTS window(device TEXT PRIMARY KEY, high INTEGER NOT NULL, bitmap TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS cursor(k INTEGER PRIMARY KEY CHECK (k = 0), after INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v INTEGER NOT NULL);
"""


def seq_accept(high: int, bitmap: int, seq: int) -> tuple[bool, int, int]:
    """The 64-wide seq window of bridge v1 (orch-relay ``ref.seq_accept``): ``(accepted, high', bitmap')``."""
    if seq > high:
        shift = seq - high
        bm = ((bitmap << shift) | 1) & ((1 << SEQ_WINDOW) - 1) if shift < SEQ_WINDOW else 1
        return True, seq, bm
    i = high - seq
    if seq < 1 or i >= SEQ_WINDOW or bitmap >> i & 1:
        return False, high, bitmap
    return True, high, bitmap | 1 << i


@dataclass(frozen=True)
class Chunk:
    idx: int
    epoch: int
    data: bytes | None  # None once dropped by retention
    state: str


@dataclass(frozen=True)
class Rid:
    device: str
    rid: str
    first_ms: int
    req_digest: str
    epoch: int
    seq: int
    state: str
    principal: str | None
    idem: str | None
    kind: str
    chunks: tuple[Chunk, ...] = ()


class Crash(BaseException):  # noqa: N818 - a test seam: a simulated kill between durable steps
    """Raised by a test's fault hook; never caught by the bridge (it is a BaseException)."""


class RidStore:
    """``rids.sqlite`` behind one connection and one lock (the relay threads share it)."""

    def __init__(self, state: StateDir, *, fault: Callable[[str], None] | None = None) -> None:
        self._fds: list[int] = []
        self._fault = fault or (lambda point: None)
        self._lock = threading.RLock()
        self._conn: sqlite3.Connection | None = None
        try:
            for name in (DB, DB + "-wal", DB + "-shm"):
                self._fds.append(state.open_private(name))
            path = state.file_path(DB)
            self._conn = sqlite3.connect(
                f"file:{path}?mode=rw", uri=True, check_same_thread=False, isolation_level=None
            )
            self._db.execute("PRAGMA auto_vacuum=INCREMENTAL")  # effective on a new file (before the first table)
            self._db.execute("PRAGMA journal_mode=WAL")
            self._db.execute("PRAGMA synchronous=FULL")
            for fd, name in zip(self._fds, (DB, DB + "-wal", DB + "-shm"), strict=True):
                held, now = os.fstat(fd), os.stat(state.file_path(name), follow_symlinks=False)
                if (held.st_dev, held.st_ino) != (now.st_dev, now.st_ino):
                    raise StateError(f"relay_state_unsafe: {name} changed under the checked open")
            self._db.executescript(_SCHEMA)
        except BaseException:
            self.close()
            raise

    def close(self) -> None:
        with self._lock:
            db, self._conn = self._conn, None
            if db is not None:
                db.close()
            while self._fds:
                os.close(self._fds.pop())

    @property
    def _db(self) -> sqlite3.Connection:
        if self._conn is None:
            raise StateError("rids.sqlite is closed")
        return self._conn

    # -- transactions ---------------------------------------------------------------------------------------------

    def _tx(self, point: str, body: Callable[[sqlite3.Connection], Any]) -> Any:
        """One transaction; the fault hook runs before ``point`` (nothing durable) and after it (committed)."""
        with self._lock:
            self._fault("before_" + point)
            db = self._db
            db.execute("BEGIN IMMEDIATE")
            try:
                out = body(db)
            except BaseException:
                db.execute("ROLLBACK")
                raise
            db.execute("COMMIT")
            self._fault("after_" + point)
            return out

    @staticmethod
    def _put_window(db: sqlite3.Connection, device: str, window: tuple[int, int] | None) -> None:
        if window is not None:
            db.execute(
                "INSERT INTO window(device, high, bitmap) VALUES(?,?,?) ON CONFLICT(device) DO UPDATE SET "
                "high=excluded.high, bitmap=excluded.bitmap",
                (device, window[0], str(window[1])),
            )

    def admit(
        self,
        *,
        device: str,
        rid: str,
        first_ms: int,
        req_digest: str,
        epoch: int,
        seq: int,
        principal: str,
        idem: str | None,
        kind: str,
        window: tuple[int, int],
        submission: tuple[str, str, Sequence[dict[str, Any]]] | None,
    ) -> None:
        """T1: the seq window, the rid record (``admitted``) and, for a write, its submission, in one transaction."""
        if kind not in ("read", "write") or (kind == "write") != (submission is not None):
            raise ValueError("admit: kind and submission")

        def body(db: sqlite3.Connection) -> None:
            self._put_window(db, device, window)
            db.execute(
                "INSERT INTO rid VALUES(?,?,?,?,?,?,'admitted',?,?,?)",
                (device, rid, first_ms, req_digest, epoch, seq, principal, idem, kind),
            )
            if submission is not None:
                op, log, events = submission
                db.execute(
                    "INSERT INTO submission VALUES(?,?,?,?,?)",
                    (device, rid, op, log, json.dumps(list(events), sort_keys=True, separators=(",", ":"))),
                )

        self._tx("T1", body)

    def record_refusal(
        self,
        *,
        device: str,
        rid: str,
        first_ms: int,
        req_digest: str,
        epoch: int,
        seq: int,
        kind: str,
        chunk: bytes,
        window: tuple[int, int] | None,
    ) -> None:
        """A refusal entered as ``answered`` before it is sent (proto §8.6 "recorded before it is sent"): the rid,
        its one chunk and, when the refusing step consumes the seq (steps 8 to 11), the window."""
        if kind not in ("refusal", "unverified"):
            raise ValueError("record_refusal: kind")

        def body(db: sqlite3.Connection) -> None:
            self._put_window(db, device, window)
            db.execute(
                "INSERT INTO rid VALUES(?,?,?,?,?,?,'answered',NULL,NULL,?)",
                (device, rid, first_ms, req_digest, epoch, seq, kind),
            )
            db.execute("INSERT INTO chunk VALUES(?,?,0,?,?,'stored')", (device, rid, epoch, chunk))

        self._tx("refusal", body)

    def answer(self, device: str, rid: str, chunks: Sequence[tuple[int, bytes]]) -> bool:
        """T2: the answer chunks (``stored``), the state ``answered`` and the deletion of the submission, in one
        transaction, before any POST. ``False`` (nothing changed) when the rid is not ``admitted``."""

        def body(db: sqlite3.Connection) -> bool:
            row = db.execute("SELECT state, epoch FROM rid WHERE device=? AND rid=?", (device, rid)).fetchone()
            if row is None or row[0] != "admitted":
                return False
            for idx, data in chunks:
                db.execute("INSERT INTO chunk VALUES(?,?,?,?,?,'stored')", (device, rid, idx, row[1], data))
            db.execute("UPDATE rid SET state='answered' WHERE device=? AND rid=?", (device, rid))
            db.execute("DELETE FROM submission WHERE device=? AND rid=?", (device, rid))
            return True

        return bool(self._tx("T2", body))

    def replace_unattempted(self, device: str, rid: str, chunks: Sequence[tuple[int, int, bytes]]) -> None:
        """Supersede every chunk of ``rid`` that was never attempted with ``chunks`` (``idx, epoch, bytes``): the
        rotation's closing refusal and the ``rid_conflict`` refusal. An attempted or posted chunk is never touched."""

        def body(db: sqlite3.Connection) -> None:
            attempted = {
                r[0]
                for r in db.execute("SELECT idx FROM chunk WHERE device=? AND rid=? AND state!='stored'", (device, rid))
            }
            if any(idx in attempted for idx, _, _ in chunks):
                raise ValueError("replace_unattempted: an attempted index")
            db.execute("DELETE FROM chunk WHERE device=? AND rid=? AND state='stored'", (device, rid))
            for idx, epoch, data in chunks:
                db.execute("INSERT INTO chunk VALUES(?,?,?,?,?,'stored')", (device, rid, idx, epoch, data))

        self._tx("supersede", body)

    def append_chunk(self, device: str, rid: str, idx: int, epoch: int, data: bytes) -> bool:
        """Ruling D: a fresh closing chunk (``already_done {status: unknown}``) for an answered rid whose bytes were
        swept, at index ``idx`` (the next unused one). ``False`` when that index exists or the rid is not answered."""

        def body(db: sqlite3.Connection) -> bool:
            row = db.execute("SELECT state FROM rid WHERE device=? AND rid=?", (device, rid)).fetchone()
            if row is None or row[0] != "answered":
                return False
            if db.execute("SELECT 1 FROM chunk WHERE device=? AND rid=? AND idx>=?", (device, rid, idx)).fetchone():
                return False
            db.execute("INSERT INTO chunk VALUES(?,?,?,?,?,'stored')", (device, rid, idx, epoch, data))
            return True

        return bool(self._tx("append", body))

    def mark(self, device: str, rid: str, idx: int, state: str) -> None:
        """A chunk's state: ``attempted`` (committed before the first byte of its first POST), ``posted`` (202),
        ``expired`` (410), ``exhausted`` (409 retry_exhausted), ``conflict`` (409 chunk_conflict), ``dropped``."""
        if state not in ("attempted", "posted", "expired", "exhausted", "conflict", "dropped"):
            raise ValueError("chunk state")

        def body(db: sqlite3.Connection) -> None:
            db.execute("UPDATE chunk SET state=? WHERE device=? AND rid=? AND idx=?", (state, device, rid, idx))

        self._tx("mark_" + state, body)

    def reopen_posting(self, device: str, rid: str) -> None:
        """An identical resend of an answered rid: its terminal chunks (``posted``/``expired``) go back to
        ``attempted`` so the stored bytes are re-POSTed (the relay stores them again within its bounds, P3-D27)."""

        def body(db: sqlite3.Connection) -> None:
            db.execute(
                "UPDATE chunk SET state='attempted' WHERE device=? AND rid=? AND state IN ('posted','expired') "
                "AND bytes IS NOT NULL",
                (device, rid),
            )

        self._tx("reopen", body)

    # -- reads ----------------------------------------------------------------------------------------------------

    def get(self, device: str, rid: str) -> Rid | None:
        with self._lock:
            row = self._db.execute("SELECT * FROM rid WHERE device=? AND rid=?", (device, rid)).fetchone()
            if row is None:
                return None
            chunks = tuple(
                Chunk(i, e, b, s)
                for i, e, b, s in self._db.execute(
                    "SELECT idx, epoch, bytes, state FROM chunk WHERE device=? AND rid=? ORDER BY idx", (device, rid)
                )
            )
            return Rid(*row, chunks=chunks)

    def window(self, device: str) -> tuple[int, int]:
        with self._lock:
            row = self._db.execute("SELECT high, bitmap FROM window WHERE device=?", (device,)).fetchone()
            return (0, 0) if row is None else (int(row[0]), int(row[1]))

    def submission(self, device: str, rid: str) -> tuple[str, str, list[dict[str, Any]]] | None:
        with self._lock:
            row = self._db.execute(
                "SELECT op, log, events FROM submission WHERE device=? AND rid=?", (device, rid)
            ).fetchone()
            return None if row is None else (row[0], row[1], json.loads(row[2]))

    def admitted(self) -> list[Rid]:
        with self._lock:
            keys = self._db.execute(
                "SELECT device, rid FROM rid WHERE state='admitted' ORDER BY first_ms, device, rid"
            ).fetchall()
        return [r for r in (self.get(d, i) for d, i in keys) if r is not None]

    def unposted(self) -> list[tuple[str, str]]:
        """``(device, rid)`` with a chunk still to POST (``stored`` or ``attempted``, bytes kept), oldest rid first.
        Keys only: the bytes are read per rid when it is posted (never every stored answer at once)."""
        with self._lock:
            rows = self._db.execute(
                "SELECT r.device, r.rid FROM rid r WHERE EXISTS (SELECT 1 FROM chunk c WHERE c.device=r.device AND "
                "c.rid=r.rid AND c.state IN ('stored','attempted') AND c.bytes IS NOT NULL) ORDER BY r.first_ms, "
                "r.device, r.rid"
            ).fetchall()
        return [(d, r) for d, r in rows]

    def answered_open(self) -> list[Rid]:
        """Answered rids with a chunk still ``stored`` or ``attempted`` (the rotation rule works on these)."""
        with self._lock:
            keys = self._db.execute(
                "SELECT DISTINCT r.device, r.rid FROM rid r JOIN chunk c ON r.device=c.device AND r.rid=c.rid "
                "WHERE r.state='answered' AND c.state IN ('stored','attempted')"
            ).fetchall()
        return [r for r in (self.get(d, i) for d, i in keys) if r is not None]

    def by_idem(self, device: str, idem: str) -> list[Rid]:
        with self._lock:
            keys = self._db.execute(
                "SELECT device, rid FROM rid WHERE device=? AND idem=? AND state!='tombstone'", (device, idem)
            ).fetchall()
        return [r for r in (self.get(d, i) for d, i in keys) if r is not None]

    def live(self, since_ms: int, device: str | None = None) -> int:
        """Records (any state but tombstone) first seen at or after ``since_ms``, for one device or all."""
        with self._lock:
            if device is None:
                q = self._db.execute("SELECT COUNT(*) FROM rid WHERE first_ms>=? AND state!='tombstone'", (since_ms,))
            else:
                q = self._db.execute(
                    "SELECT COUNT(*) FROM rid WHERE device=? AND first_ms>=? AND state!='tombstone'", (device, since_ms)
                )
            return int(q.fetchone()[0])

    def chunk_bytes(self, device: str | None = None) -> int:
        """Stored chunk bytes (not yet swept), for one device or host-wide (the byte budget, R2-4)."""
        with self._lock:
            if device is None:
                q = self._db.execute("SELECT COALESCE(SUM(LENGTH(bytes)), 0) FROM chunk WHERE bytes IS NOT NULL")
            else:
                q = self._db.execute(
                    "SELECT COALESCE(SUM(LENGTH(bytes)), 0) FROM chunk WHERE device=? AND bytes IS NOT NULL", (device,)
                )
            return int(q.fetchone()[0])

    def admitted_reads(self, device: str | None = None) -> int:
        """Admitted reads (each may still store up to one whole answer), for one device or host-wide."""
        with self._lock:
            if device is None:
                q = self._db.execute("SELECT COUNT(*) FROM rid WHERE state='admitted' AND kind='read'")
            else:
                q = self._db.execute(
                    "SELECT COUNT(*) FROM rid WHERE device=? AND state='admitted' AND kind='read'", (device,)
                )
            return int(q.fetchone()[0])

    def pragmas(self) -> dict[str, Any]:
        """The durability settings (a test pins them: WAL, ``synchronous=FULL``, ``auto_vacuum=INCREMENTAL``)."""
        with self._lock:
            return {
                k: self._db.execute(f"PRAGMA {k}").fetchone()[0] for k in ("journal_mode", "synchronous", "auto_vacuum")
            }

    # -- cursor and the start fence -------------------------------------------------------------------------------

    def cursor(self) -> int:
        with self._lock:
            row = self._db.execute("SELECT after FROM cursor WHERE k=0").fetchone()
            return 0 if row is None else int(row[0])

    def set_cursor(self, after: int) -> None:
        if after < self.cursor():
            raise ValueError("the cursor never moves back")
        self._tx(
            "cursor",
            lambda db: db.execute(
                "INSERT INTO cursor(k, after) VALUES(0, ?) ON CONFLICT(k) DO UPDATE SET after=excluded.after", (after,)
            ),
        )

    def start_ms(self) -> int | None:
        with self._lock:
            row = self._db.execute("SELECT v FROM meta WHERE k='start_ms'").fetchone()
            return None if row is None else int(row[0])

    def set_start_ms(self, ms: int) -> None:
        self._tx(
            "start",
            lambda db: db.execute(
                "INSERT INTO meta(k, v) VALUES('start_ms', ?) ON CONFLICT(k) DO UPDATE SET v=excluded.v", (ms,)
            ),
        )

    # -- retention (wall clock, 11A.8) ----------------------------------------------------------------------------

    def sweep(self, now_ms: int) -> None:
        """Chunk bytes go at ``first_ms + 960 s``; an answered rid becomes a tombstone ``{device, rid, req_digest}``
        24 h after ``first_ms`` and goes 7 days later. An ``admitted`` rid is never touched; the window never goes."""

        def body(db: sqlite3.Connection) -> None:
            db.execute(
                "UPDATE chunk SET bytes=NULL WHERE bytes IS NOT NULL AND EXISTS (SELECT 1 FROM rid r WHERE "
                "r.device=chunk.device AND r.rid=chunk.rid AND r.first_ms + ? <= ?)",
                (CHUNK_KEEP_MS, now_ms),
            )
            db.execute(
                "DELETE FROM chunk WHERE EXISTS (SELECT 1 FROM rid r WHERE r.device=chunk.device AND r.rid=chunk.rid "
                "AND r.state='answered' AND r.first_ms + ? <= ?)",
                (RID_KEEP_MS, now_ms),
            )
            db.execute(
                "UPDATE rid SET state='tombstone', principal=NULL, idem=NULL WHERE state='answered' "
                "AND first_ms + ? <= ?",
                (RID_KEEP_MS, now_ms),
            )
            db.execute(
                "DELETE FROM rid WHERE state='tombstone' AND first_ms + ? <= ?",
                (RID_KEEP_MS + TOMBSTONE_KEEP_MS, now_ms),
            )
            over = db.execute("SELECT COUNT(*) FROM rid WHERE state='tombstone'").fetchone()[0] - MAX_TOMBSTONES
            if over > 0:  # the oldest first: the seq window and the ±300 s check still refuse an old request
                db.execute(
                    "DELETE FROM rid WHERE rowid IN (SELECT rowid FROM rid WHERE state='tombstone' "
                    "ORDER BY first_ms LIMIT ?)",
                    (over,),
                )

        self._tx("sweep", body)
        with self._lock:
            self._db.execute(f"PRAGMA incremental_vacuum({VACUUM_PAGES})").fetchall()

    def dump(self) -> dict[str, Iterable[Any]]:  # tests and the doctor: no chunk bytes, no events
        with self._lock:
            return {
                "rid": self._db.execute(
                    "SELECT device, rid, state, kind, seq FROM rid ORDER BY device, rid"
                ).fetchall(),
                "chunk": self._db.execute(
                    "SELECT device, rid, idx, state, bytes IS NOT NULL FROM chunk ORDER BY device, rid, idx"
                ).fetchall(),
                "submission": self._db.execute("SELECT device, rid FROM submission").fetchall(),
            }
