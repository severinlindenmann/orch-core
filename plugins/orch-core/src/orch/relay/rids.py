"""The rid store of R2 (contract 11A.3 ``rids.sqlite``, 11A.4 "The rid state machine"): the durable exactly-once record.

Tables::

    rid(device, rid, first_ms, req_digest, epoch, seq, state, principal, idem, kind, reopens)
                                                   state: admitted|answered|tombstone
    submission(device, rid, op, log, events)       the device-signed events of an admitted write (11A.3 exception)
    chunk(device, rid, idx, epoch, bytes, state)   state: stored|attempted|posted|expired|exhausted|conflict|dropped
    window(device, high, bitmap)                   the seq window (64), never dropped with the rids
    cursor(after)                                  the poll cursor: end of the contiguous durable prefix
    meta(k, v)                                     start_ms (the start fence)

``kind``: ``read``/``write`` (admitted to run), ``refusal`` (refused after step 5, answered at entry), ``unverified``
(a step-4 refusal: device, signature; recorded so a resend replays the same bytes, never consumes a seq).

``reopens`` counts the replays that re-opened an answered rid for posting (P3-D27's 8 re-stores per rid): kept in the
row, so the bound holds across restarts and never depends on an in-memory tracker (review N3-3).

``device`` is the verified device id, except for ``unverified`` records: those are keyed ``u:<header device>`` (R2-11),
so a request a verified device sends never matches a refusal recorded for a forged header naming it.

**Growth bound** (R2-3/R2-4; the caps are the bridge's, checked before every insert): rows per device per 15 min
``PER_DEVICE + BUSY_ALLOWANCE`` (1 088) and per 24 h ``DEVICE_DAY_ROWS`` (16 384); host-wide per 15 min
``MAX_RECORDS`` (16 384) and per 24 h ``MAX_RETAINED_ROWS`` (262 144); tombstones at most :data:`MAX_TOMBSTONES` (the
oldest go first; the seq window and the ±300 s check keep refusing an old request without them); stored chunk bytes
per device and host-wide within the bridge's byte budgets. ``auto_vacuum=INCREMENTAL``: every sweep returns free pages
to the file system (:data:`VACUUM_PAGES` at a time, :data:`VACUUM_STEP` per lock hold).

**Counts in memory** (review D2-6): the host-wide live-row count and the stored chunk bytes (host-wide and per
device) are kept in memory, read from the file at open and updated by every transaction that changes them, so the
bridge's per-request caps never scan the table; the per-device row counts use the ``(device, first_ms)`` index (at
most a day of one device's rows). :meth:`sweep` works in batches of :data:`SWEEP_BATCH` rows, one short transaction
each, so no relay thread waits behind it for long; every batch resumes from a cursor (the last row it took, in index
order), so a sweep reads each row at most once (no rescans: linear, review perf of delta 3).

Every transition is ONE SQLite transaction (``synchronous=FULL``, WAL): T1 :meth:`admit` (window + rid + submission),
T2 :meth:`answer` (chunks ``stored`` + state ``answered`` + submission deleted), the refusals (:meth:`record_refusal`:
rid ``answered`` + its chunk, and the window when the step consumes the seq), and the chunk marks. Nothing is ever
POSTed before the transaction that stores its bytes has committed; an ``attempted`` chunk's bytes are frozen.

**Opening** follows 11A.3: the checked open of the database and both sidecars relative to the locked state directory
(``O_NOFOLLOW``, 0600, owner, ``nlink == 1``; the fds stay open), ``sqlite3.connect("file:...?mode=rw")``, then a
``stat`` of every path compared with the held fds (``(st_dev, st_ino)``); a mismatch refuses (``relay_state_unsafe``).
"""

from __future__ import annotations

import bisect
import json
import os
import sqlite3
import threading
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from typing import Any

from orch.wsproto.state import StateDir, StateError

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
VACUUM_STEP = 256  # pages per lock hold
SWEEP_BATCH = 2048  # rows per sweep transaction (the RidStore lock is released between batches)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS rid(device TEXT NOT NULL, rid TEXT NOT NULL, first_ms INTEGER NOT NULL,
  req_digest TEXT NOT NULL, epoch INTEGER NOT NULL, seq INTEGER NOT NULL, state TEXT NOT NULL, principal TEXT,
  idem TEXT, kind TEXT NOT NULL, reopens INTEGER NOT NULL DEFAULT 0, PRIMARY KEY(device, rid));
CREATE INDEX IF NOT EXISTS rid_idem ON rid(device, idem);
CREATE INDEX IF NOT EXISTS rid_dev_first ON rid(device, first_ms);
CREATE INDEX IF NOT EXISTS rid_first ON rid(first_ms);
CREATE INDEX IF NOT EXISTS rid_admitted ON rid(device, kind) WHERE state='admitted';
CREATE INDEX IF NOT EXISTS rid_state_first ON rid(state, first_ms);
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
    reopens: int = 0
    chunks: tuple[Chunk, ...] = ()


class Crash(BaseException):  # noqa: N818 - a test seam: a simulated kill between durable steps
    """Raised by a test's fault hook; never caught by the bridge (it is a BaseException)."""


def _atomic(db: sqlite3.Connection, body: Callable[[sqlite3.Connection], Any]) -> Any:
    """THE transaction of this module (every BEGIN/COMMIT/ROLLBACK is here; a test pins it): ``BEGIN IMMEDIATE``, the
    body, ``COMMIT`` inside the same ``try``. On ANY exception (the body's or a failed ``COMMIT``) the transaction is
    rolled back if still open, so the writer lock is never left held (every later ``BEGIN IMMEDIATE`` would fail); a
    rollback error is swallowed and the original exception re-raised."""
    db.execute("BEGIN IMMEDIATE")
    try:
        out = body(db)
        db.execute("COMMIT")
    except BaseException:
        if db.in_transaction:
            try:
                db.execute("ROLLBACK")
            except sqlite3.Error:
                pass  # the original error is the one that matters
        raise
    return out


class RidStore:
    """``rids.sqlite`` behind one connection and one lock (the relay threads share it)."""

    def __init__(self, state: StateDir, *, fault: Callable[[str], None] | None = None) -> None:
        self._fds: list[int] = []
        self._fault = fault or (lambda point: None)
        self._lock = threading.RLock()
        self._conn: sqlite3.Connection | None = None
        self._live: list[int] = []  # sorted first_ms of every non-tombstone row (host-wide counts, D2-6)
        self._bytes: dict[str, int] = {}  # stored chunk bytes per device key (bytes not yet swept)
        self._bytes_total = 0
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
            self._migrate()
            self._db.executescript(_SCHEMA)
            self._load_counts()
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

    def _migrate(self) -> None:
        """A file from before ``reopens`` (round 3) gets the column; a new file gets it from the schema."""
        cols = {r[1] for r in self._db.execute("PRAGMA table_info(rid)")}
        if cols and "reopens" not in cols:
            self._db.execute("ALTER TABLE rid ADD COLUMN reopens INTEGER NOT NULL DEFAULT 0")

    def _load_counts(self) -> None:
        with self._lock:
            self._live = [
                r[0] for r in self._db.execute("SELECT first_ms FROM rid WHERE state!='tombstone' ORDER BY first_ms")
            ]
            self._bytes = {
                d: int(n)
                for d, n in self._db.execute(
                    "SELECT device, SUM(LENGTH(bytes)) FROM chunk WHERE bytes IS NOT NULL GROUP BY device"
                )
            }
            self._bytes_total = sum(self._bytes.values())

    def _add_bytes(self, device: str, n: int) -> None:
        if n:
            left = self._bytes.get(device, 0) + n
            if left > 0:
                self._bytes[device] = left
            else:
                self._bytes.pop(device, None)
            self._bytes_total += n

    def _drop_live(self, first_ms: int) -> None:
        i = bisect.bisect_left(self._live, first_ms)
        if i < len(self._live) and self._live[i] == first_ms:
            del self._live[i]

    def _add_live(self, first_ms: int) -> None:
        if not self._live or first_ms >= self._live[-1]:
            self._live.append(first_ms)
        else:
            bisect.insort(self._live, first_ms)

    # -- transactions ---------------------------------------------------------------------------------------------

    def _tx(self, point: str, body: Callable[[sqlite3.Connection], Any]) -> Any:
        """One transaction; the fault hook runs before ``point`` (nothing durable) and after it (committed)."""
        with self._lock:
            self._fault("before_" + point)
            out = _atomic(self._db, body)
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
                "INSERT INTO rid(device, rid, first_ms, req_digest, epoch, seq, state, principal, idem, kind) "
                "VALUES(?,?,?,?,?,?,'admitted',?,?,?)",
                (device, rid, first_ms, req_digest, epoch, seq, principal, idem, kind),
            )
            if submission is not None:
                op, log, events = submission
                db.execute(
                    "INSERT INTO submission VALUES(?,?,?,?,?)",
                    (device, rid, op, log, json.dumps(list(events), sort_keys=True, separators=(",", ":"))),
                )

        with self._lock:
            self._tx("T1", body)
            self._add_live(first_ms)

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
                "INSERT INTO rid(device, rid, first_ms, req_digest, epoch, seq, state, principal, idem, kind) "
                "VALUES(?,?,?,?,?,?,'answered',NULL,NULL,?)",
                (device, rid, first_ms, req_digest, epoch, seq, kind),
            )
            db.execute("INSERT INTO chunk VALUES(?,?,0,?,?,'stored')", (device, rid, epoch, chunk))

        with self._lock:
            self._tx("refusal", body)
            self._add_live(first_ms)
            self._add_bytes(device, len(chunk))

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

        with self._lock:
            done = bool(self._tx("T2", body))
            if done:
                self._add_bytes(device, sum(len(d) for _, d in chunks))
            return done

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
            gone = db.execute(
                "SELECT COALESCE(SUM(LENGTH(bytes)), 0) FROM chunk WHERE device=? AND rid=? AND state='stored'",
                (device, rid),
            ).fetchone()[0]
            db.execute("DELETE FROM chunk WHERE device=? AND rid=? AND state='stored'", (device, rid))
            for idx, epoch, data in chunks:
                db.execute("INSERT INTO chunk VALUES(?,?,?,?,?,'stored')", (device, rid, idx, epoch, data))
            return sum(len(d) for _, _, d in chunks) - int(gone)

        with self._lock:
            self._add_bytes(device, self._tx("supersede", body))

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

        with self._lock:
            done = bool(self._tx("append", body))
            if done:
                self._add_bytes(device, len(data))
            return done

    def note_reopen(self, device: str, rid: str, limit: int) -> bool:
        """Count one more replay that re-opens ``rid`` for posting; ``False`` (nothing changed) once ``limit`` replays
        were counted, or when the rid is not answered. Durable (review N3-3): no eviction, no restart resets it."""

        def body(db: sqlite3.Connection) -> bool:
            return (
                db.execute(
                    "UPDATE rid SET reopens=reopens+1 WHERE device=? AND rid=? AND state='answered' AND reopens<?",
                    (device, rid, limit),
                ).rowcount
                == 1
            )

        return bool(self._tx("reopen_count", body))

    def abandon(self, device: str, rid: str) -> bool:
        """An admitted READ nothing can answer any more (its epoch key is gone): a tombstone now, so it no longer
        holds a byte reserve or an admitted row (review N3-2). ``False`` when it is no longer an admitted read."""

        def body(db: sqlite3.Connection) -> int | None:
            row = db.execute(
                "SELECT first_ms FROM rid WHERE device=? AND rid=? AND state='admitted' AND kind='read'", (device, rid)
            ).fetchone()
            if row is None:
                return None
            db.execute(
                "UPDATE rid SET state='tombstone', principal=NULL, idem=NULL WHERE device=? AND rid=?", (device, rid)
            )
            return int(row[0])

        with self._lock:
            first = self._tx("abandon", body)
            if first is None:
                return False
            self._drop_live(first)
            return True

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
        """Records (any state but tombstone) first seen at or after ``since_ms``, for one device or all (host-wide: from
        memory, D2-6)."""
        with self._lock:
            if device is None:
                return len(self._live) - bisect.bisect_left(self._live, since_ms)
            else:
                q = self._db.execute(
                    "SELECT COUNT(*) FROM rid WHERE device=? AND first_ms>=? AND state!='tombstone'", (device, since_ms)
                )
            return int(q.fetchone()[0])

    def chunk_bytes(self, device: str | None = None) -> int:
        """Stored chunk bytes (not yet swept), for one device or host-wide (the byte budget, R2-4); from memory."""
        with self._lock:
            return self._bytes_total if device is None else self._bytes.get(device, 0)

    def admitted_reads(self, device: str | None = None, *, since_ms: int | None = None) -> int:
        """Admitted reads (each may still store up to one whole answer), for one device or host-wide; with
        ``since_ms`` only those first seen at or after it (an older one can no longer store an answer the relay
        takes, review N3-2)."""
        since = -(1 << 62) if since_ms is None else since_ms
        with self._lock:
            if device is None:
                q = self._db.execute(
                    "SELECT COUNT(*) FROM rid WHERE state='admitted' AND kind='read' AND first_ms>=?", (since,)
                )
            else:
                q = self._db.execute(
                    "SELECT COUNT(*) FROM rid WHERE device=? AND state='admitted' AND kind='read' AND first_ms>=?",
                    (device, since),
                )
            return int(q.fetchone()[0])

    def stale_reads(self, before_ms: int, limit: int = 256) -> list[Rid]:
        """Admitted reads first seen before ``before_ms``, oldest first (at most ``limit``)."""
        with self._lock:
            keys = self._db.execute(
                "SELECT device, rid FROM rid WHERE state='admitted' AND kind='read' AND first_ms<? "
                "ORDER BY first_ms, device, rid LIMIT ?",
                (before_ms, limit),
            ).fetchall()
        return [r for r in (self.get(d, i) for d, i in keys) if r is not None]

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
        24 h after ``first_ms`` and goes 7 days later. An ``admitted`` rid is never touched; the window never goes.
        In batches of :data:`SWEEP_BATCH` rows, one transaction each (the lock is free between them, D2-6)."""
        chunk_cut, rid_cut = now_ms - CHUNK_KEEP_MS, now_ms - RID_KEEP_MS
        # each step resumes after the last row it took (index order), so no batch reads a row an earlier one read
        cur: list[Any] = ["", 0]

        def drop_bytes(db: sqlite3.Connection) -> list[Any]:
            rows = db.execute(
                "SELECT c.rowid, c.device, LENGTH(c.bytes) FROM chunk c INDEXED BY chunk_live JOIN rid r ON "
                "r.device=c.device AND r.rid=c.rid WHERE c.bytes IS NOT NULL AND (c.device, c.rowid) > (?, ?) "
                "AND r.first_ms<=? ORDER BY c.device, c.rowid LIMIT ?",
                (cur[0], cur[1], chunk_cut, SWEEP_BATCH),
            ).fetchall()
            db.executemany("UPDATE chunk SET bytes=NULL WHERE rowid=?", [(r[0],) for r in rows])
            return rows

        def tombstone(db: sqlite3.Connection) -> list[Any]:
            """The chunks of a batch of answered rids past 24 h go, and those rids become tombstones, together."""
            keys = db.execute(
                "SELECT rowid, device, rid, first_ms FROM rid INDEXED BY rid_state_first WHERE state='answered' AND "
                "first_ms<=? AND (first_ms, rowid) > (?, ?) ORDER BY first_ms, rowid LIMIT ?",
                (rid_cut, cur[0], cur[1], SWEEP_BATCH),
            ).fetchall()
            gone: list[Any] = []
            for rowid, device, rid, _ in keys:
                n = db.execute(
                    "SELECT COALESCE(SUM(LENGTH(bytes)), 0) FROM chunk WHERE device=? AND rid=?", (device, rid)
                ).fetchone()[0]
                db.execute("DELETE FROM chunk WHERE device=? AND rid=?", (device, rid))
                gone.append((rowid, device, int(n)))
            db.executemany(
                "UPDATE rid SET state='tombstone', principal=NULL, idem=NULL WHERE rowid=?", [(k[0],) for k in keys]
            )
            if keys:
                cur[0], cur[1] = keys[-1][3], keys[-1][0]
            return gone

        def forget(db: sqlite3.Connection) -> int:
            return db.execute(
                "DELETE FROM rid WHERE rowid IN (SELECT rowid FROM rid INDEXED BY rid_state_first WHERE "
                "state='tombstone' AND first_ms<=? ORDER BY first_ms LIMIT ?)",
                (now_ms - RID_KEEP_MS - TOMBSTONE_KEEP_MS, SWEEP_BATCH),
            ).rowcount

        def over_cap(n: int) -> Callable[[sqlite3.Connection], int]:
            def body(db: sqlite3.Connection) -> int:
                # the oldest first: the seq window and the ±300 s check still refuse an old request
                return db.execute(
                    "DELETE FROM rid WHERE rowid IN (SELECT rowid FROM rid INDEXED BY rid_state_first WHERE "
                    "state='tombstone' ORDER BY first_ms LIMIT ?)",
                    (n,),
                ).rowcount

            return body

        while True:
            with self._lock:  # the counts change with the committed transaction, never before it
                rows = self._tx("sweep", drop_bytes)
                for _, device, n in rows:
                    self._add_bytes(device, -int(n))
            if len(rows) < SWEEP_BATCH:
                break
            cur[0], cur[1] = rows[-1][1], rows[-1][0]
        cur[0], cur[1] = -(1 << 62), 0
        while True:
            with self._lock:
                gone = self._tx("sweep", tombstone)
                for _, device, n in gone:
                    self._add_bytes(device, -n)
            if len(gone) < SWEEP_BATCH:
                break
        while self._tx("sweep", forget) >= SWEEP_BATCH:
            pass
        with self._lock:
            over = self._db.execute("SELECT COUNT(*) FROM rid WHERE state='tombstone'").fetchone()[0] - MAX_TOMBSTONES
        while over > 0:
            done = self._tx("sweep", over_cap(min(over, SWEEP_BATCH)))
            if done <= 0:
                break
            over -= done
        with self._lock:  # the tombstoned rows leave the in-memory live list (survivors: the admitted ones)
            cut = bisect.bisect_right(self._live, rid_cut)
            if cut:
                kept = [
                    r[0]
                    for r in self._db.execute(
                        "SELECT first_ms FROM rid WHERE state!='tombstone' AND first_ms<=? ORDER BY first_ms",
                        (rid_cut,),
                    )
                ]
                self._live = kept + self._live[cut:]
        for _ in range(VACUUM_PAGES // VACUUM_STEP):
            with self._lock:
                if self._vacuum_step():
                    break

    def _vacuum_step(self) -> bool:
        """Return at most :data:`VACUUM_STEP` free pages in one transaction (the caller holds the lock); True when none
        are left. One page per ``PRAGMA incremental_vacuum(1)``: ``execute(...).fetchall()`` steps a pragma without
        result columns only ONCE on Python 3.11, so ``incremental_vacuum(N)`` there frees one page, not N. Every
        transaction of this connection is a :meth:`_tx` that ends under the same lock, so none is open here; an open
        one is refused, never committed or joined."""
        db = self._db
        if db.in_transaction:
            raise StateError("relay_state_unsafe: a transaction is open at the vacuum step")

        def pages(db: sqlite3.Connection) -> None:
            for _ in range(VACUUM_STEP):
                if db.execute("PRAGMA freelist_count").fetchone()[0] == 0:
                    break
                db.execute("PRAGMA incremental_vacuum(1)").fetchall()

        _atomic(db, pages)
        return db.execute("PRAGMA freelist_count").fetchone()[0] == 0

    def counts(self) -> dict[str, Any]:
        """The in-memory counts and the same counts from the file (a test keeps them equal)."""
        with self._lock:
            db = self._db
            return {
                "memory": (len(self._live), self._bytes_total, dict(self._bytes)),
                "file": (
                    db.execute("SELECT COUNT(*) FROM rid WHERE state!='tombstone'").fetchone()[0],
                    db.execute("SELECT COALESCE(SUM(LENGTH(bytes)), 0) FROM chunk WHERE bytes IS NOT NULL").fetchone()[
                        0
                    ],
                    {
                        d: n
                        for d, n in db.execute(
                            "SELECT device, SUM(LENGTH(bytes)) FROM chunk WHERE bytes IS NOT NULL GROUP BY device"
                        )
                    },
                ),
            }

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
