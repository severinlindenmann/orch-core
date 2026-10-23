"""``relay/outbound.sqlite``: the host's durable push outbox (orch-v2-host 11A.16.3, H13a; "As built in B4").

Tables (none of it is F1)::

    meta(k, v)                      start_ms (the start fence), instance_id
    push_cursor(log, seq, head)     the last event judged per log; advanced in the same transaction as its intents
    push_intent(key PK, ...)        the planned/sealed/final pushes; ``deadline_ms`` is immutable; the sealed ``outer``
                                    is persisted inside the seal span, before the first send, and never changes
                                    except by a recorded reseal
    push_ts(id_hash PK, last_ts, seen_ms)   the last ``ts_ms`` per id (Q-H3); rows older than 25 h are pruned

``state``: ``planned`` (to be sealed), ``sealed`` (outer persisted; to be sent, resent unchanged), and the final
states ``sent``, ``dropped``, ``failed``, ``merged`` (a newer intent of the same id took its place), ``invalidated``
(its event is no longer in the log). A final state never moves again and its ``label`` is cleared (the label text
is user content: it lives only while the push can still be sent).

**Opening** is R2's checked open (:class:`orch.relay.rids.RidStore`): the database and both sidecars relative to the
locked state directory (``O_NOFOLLOW``, 0600, owner, ``nlink == 1``; the fds stay open), ``sqlite3.connect``, WAL,
``synchronous=FULL``, then a ``stat`` of every path compared with the held fds. A different ``instance_id`` (a restore
or a copied directory) clears the tables and starts a new fence at ``now`` ("restore", 11A.16.3: the spec renames the
files aside; this clears them, the lost rows being unsendable anyway, a follow-up).

Every transition is ONE SQLite transaction (:func:`_atomic`). The store has one lock, the innermost of the push
path (11A.16.2 "Lock order"): it never calls out.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import threading
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from typing import Any

from orch.wsproto.state import StateDir, StateError

__all__ = [
    "DB",
    "FINAL",
    "PUSH_TS_KEEP_MS",
    "SCHEMA_VERSION",
    "OutboxVersionError",
    "Intent",
    "NewIntent",
    "OutboundStore",
    "id_hash",
    "intent_key",
]

DB = "outbound.sqlite"
SCHEMA_VERSION = 1  # PRAGMA user_version; a database written by a NEWER version is refused on open
FINAL = ("sent", "dropped", "failed", "merged", "invalidated")
PUSH_TS_KEEP_MS = 25 * 3600 * 1000

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS push_cursor(log TEXT PRIMARY KEY, seq INTEGER NOT NULL, head TEXT);
CREATE TABLE IF NOT EXISTS push_intent(
  n INTEGER PRIMARY KEY AUTOINCREMENT, key TEXT NOT NULL UNIQUE, log TEXT NOT NULL, seq INTEGER NOT NULL,
  event_hash TEXT NOT NULL, kind TEXT NOT NULL, id TEXT NOT NULL, ticket TEXT, narrow TEXT, exclude TEXT NOT NULL,
  label TEXT NOT NULL, priority TEXT NOT NULL, deadline_ms INTEGER NOT NULL, state TEXT NOT NULL,
  targets TEXT, auth_gen INTEGER, outer BLOB, epoch INTEGER, ts_ms INTEGER, sealed_ms INTEGER,
  attempts INTEGER NOT NULL DEFAULT 0, reseals INTEGER NOT NULL DEFAULT 0, next_ms INTEGER NOT NULL DEFAULT 0,
  sent_ms INTEGER, accepted INTEGER, reason TEXT, created_ms INTEGER NOT NULL);
CREATE INDEX IF NOT EXISTS push_intent_state ON push_intent(state, n);
CREATE INDEX IF NOT EXISTS push_intent_log ON push_intent(log, seq);
CREATE INDEX IF NOT EXISTS push_intent_id ON push_intent(id, state);
CREATE INDEX IF NOT EXISTS push_intent_sent ON push_intent(sent_ms) WHERE sent_ms IS NOT NULL;
CREATE TABLE IF NOT EXISTS push_ts(id_hash TEXT PRIMARY KEY, last_ts INTEGER NOT NULL, seen_ms INTEGER NOT NULL);
"""

_COLS = (
    "key, log, seq, event_hash, kind, id, ticket, narrow, exclude, label, priority, deadline_ms, state, targets, "
    "auth_gen, outer, epoch, ts_ms, sealed_ms, attempts, reseals, next_ms, sent_ms, accepted, reason, created_ms"
)


class OutboxVersionError(StateError):
    """``outbound.sqlite`` was written by a newer orch (its ``user_version`` is above :data:`SCHEMA_VERSION`)."""


def intent_key(log: str, seq: int, event_hash: str, kind: str, id_: str) -> str:
    """``key = H(log ‖ seq ‖ event_hash ‖ kind ‖ id)`` (11A.16.3), length-prefixed so no two tuples collide."""
    h = hashlib.sha256()
    for part in (log, str(seq), event_hash, kind, id_):
        b = part.encode("utf-8")
        h.update(len(b).to_bytes(4, "big") + b)
    return h.hexdigest()


def id_hash(id_: str) -> str:
    return hashlib.sha256(b"orch/v2/push-id|" + id_.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class NewIntent:
    """A planned push (the planner's output): everything but the seal."""

    log: str
    seq: int
    event_hash: str
    kind: str
    id: str
    ticket: str | None
    narrow: frozenset[str] | None
    exclude: frozenset[str]
    label: str
    priority: str
    deadline_ms: int
    created_ms: int

    @property
    def key(self) -> str:
        return intent_key(self.log, self.seq, self.event_hash, self.kind, self.id)


@dataclass(frozen=True)
class Intent:
    key: str
    log: str
    seq: int
    event_hash: str
    kind: str
    id: str
    ticket: str | None
    narrow: frozenset[str] | None
    exclude: frozenset[str]
    label: str
    priority: str
    deadline_ms: int
    state: str
    targets: tuple[str, ...] | None
    auth_gen: int | None
    outer: bytes | None
    epoch: int | None
    ts_ms: int | None
    sealed_ms: int | None
    attempts: int
    reseals: int
    next_ms: int
    sent_ms: int | None
    accepted: int | None
    reason: str | None
    created_ms: int


def _intent(row: Sequence[Any]) -> Intent:
    (key, log, seq, eh, kind, id_, ticket, narrow, exclude, label, prio, deadline, state, targets, auth_gen, outer,
     epoch, ts_ms, sealed_ms, attempts, reseals, next_ms, sent_ms, accepted, reason, created) = row  # fmt: skip
    return Intent(
        key, log, seq, eh, kind, id_, ticket, None if narrow is None else frozenset(json.loads(narrow)),
        frozenset(json.loads(exclude)), label, prio, deadline, state,
        None if targets is None else tuple(json.loads(targets)), auth_gen, None if outer is None else bytes(outer),
        epoch, ts_ms, sealed_ms, attempts, reseals, next_ms, sent_ms, accepted, reason, created,
    )  # fmt: skip


def _atomic(db: sqlite3.Connection, body: Callable[[sqlite3.Connection], Any]) -> Any:
    """THE transaction of this module (every BEGIN/COMMIT/ROLLBACK is here): ``BEGIN IMMEDIATE``, the body,
    ``COMMIT`` inside the same ``try``; any exception rolls back (a rollback error is swallowed)."""
    db.execute("BEGIN IMMEDIATE")
    try:
        out = body(db)
        db.execute("COMMIT")
    except BaseException:
        if db.in_transaction:
            try:
                db.execute("ROLLBACK")
            except sqlite3.Error:
                pass
        raise
    return out


class OutboundStore:
    """``outbound.sqlite`` behind one connection and one lock."""

    def __init__(self, state: StateDir, *, instance_id: str, now_ms: int) -> None:
        self._fds: list[int] = []
        self._lock = threading.RLock()
        self._conn: sqlite3.Connection | None = None
        self.restored = False  # the instance id differed: the tables were cleared and the fence restarted
        try:
            for name in (DB, DB + "-wal", DB + "-shm"):
                self._fds.append(state.open_private(name))
            path = state.file_path(DB)
            self._conn = sqlite3.connect(
                f"file:{path}?mode=rw", uri=True, check_same_thread=False, isolation_level=None
            )
            self._db.execute("PRAGMA journal_mode=WAL")
            self._db.execute("PRAGMA synchronous=FULL")
            for fd, name in zip(self._fds, (DB, DB + "-wal", DB + "-shm"), strict=True):
                held, now = os.fstat(fd), os.stat(state.file_path(name), follow_symlinks=False)
                if (held.st_dev, held.st_ino) != (now.st_dev, now.st_ino):
                    raise StateError(f"relay_state_unsafe: {name} changed under the checked open")
            have = int(self._db.execute("PRAGMA user_version").fetchone()[0])
            if have > SCHEMA_VERSION:
                raise OutboxVersionError(
                    f"outbound_schema_newer: {DB} has schema version {have}, this orch knows {SCHEMA_VERSION}"
                )
            self._db.executescript(_SCHEMA)
            if have < SCHEMA_VERSION:
                self._db.execute(f"PRAGMA user_version={int(SCHEMA_VERSION)}")
            self._init_meta(instance_id, now_ms)
        except BaseException:
            self.close()
            raise

    @property
    def _db(self) -> sqlite3.Connection:
        if self._conn is None:
            raise StateError("outbound.sqlite is closed")
        return self._conn

    def close(self) -> None:
        with self._lock:
            db, self._conn = self._conn, None
            if db is not None:
                db.close()
            while self._fds:
                os.close(self._fds.pop())

    def _init_meta(self, instance_id: str, now_ms: int) -> None:
        def body(db: sqlite3.Connection) -> None:
            rows = dict(db.execute("SELECT k, v FROM meta"))
            if rows.get("instance_id") not in (None, instance_id) or "start_ms" not in rows:
                if "start_ms" in rows:
                    self.restored = True
                for t in ("push_cursor", "push_intent", "push_ts", "meta"):
                    db.execute(f"DELETE FROM {t}")
                db.execute("INSERT INTO meta(k, v) VALUES ('start_ms', ?), ('instance_id', ?)",
                           (str(now_ms), instance_id))  # fmt: skip

        with self._lock:
            _atomic(self._db, body)

    @property
    def start_ms(self) -> int:
        with self._lock:
            row = self._db.execute("SELECT v FROM meta WHERE k='start_ms'").fetchone()
            return int(row[0])

    # -- cursors and planning ----------------------------------------------------------------------------------------

    def cursor(self, log: str) -> tuple[int, str | None] | None:
        with self._lock:
            row = self._db.execute("SELECT seq, head FROM push_cursor WHERE log=?", (log,)).fetchone()
            return None if row is None else (row[0], row[1])

    def plan(
        self,
        log: str,
        seq: int,
        head: str | None,
        intents: Iterable[NewIntent] = (),
        *,
        invalidate: Iterable[str] = (),
    ) -> int:
        """ONE transaction (11A.16.4 step 4): ``INSERT OR IGNORE`` each intent on its key (a crash before this
        commits replans the same keys, one after finds them), merge an older ``planned`` intent of the same id into
        the new one, set ``invalidate`` keys ``invalidated`` (reason ``abandoned``), and move the cursor. Returns the
        number of intents inserted."""
        items = list(intents)

        def body(db: sqlite3.Connection) -> int:
            n = 0
            for k in invalidate:
                db.execute(
                    f"UPDATE push_intent SET state='invalidated', reason='abandoned', label='' WHERE key=? "
                    f"AND state NOT IN ({','.join('?' * len(FINAL))})",
                    (k, *FINAL),
                )
            for it in items:
                older = db.execute(
                    "SELECT key, kind FROM push_intent WHERE id=? AND state='planned' AND key<>?", (it.id, it.key)
                ).fetchall()
                # a question answered before its push was sealed: both are merged, nothing is shown (11A.16.4
                # "JUDGEMENT CALL (merge)"); any other newer intent of an id replaces the older unsent one
                both = it.kind == "question.closed" and any(k == "question" for _, k in older)
                cur = db.execute(
                    "INSERT OR IGNORE INTO push_intent(key, log, seq, event_hash, kind, id, ticket, narrow, exclude, "
                    "label, priority, deadline_ms, state, reason, created_ms) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (it.key, it.log, it.seq, it.event_hash, it.kind, it.id, it.ticket,
                     None if it.narrow is None else json.dumps(sorted(it.narrow)), json.dumps(sorted(it.exclude)),
                     "" if both else it.label, it.priority, it.deadline_ms, "merged" if both else "planned",
                     "answered_before_seal" if both else None, it.created_ms),
                )  # fmt: skip
                if cur.rowcount:
                    n += 1
                    for k, _ in older:
                        db.execute("UPDATE push_intent SET state='merged', reason=?, label='' WHERE key=?",
                                   ("answered_before_seal" if both else "newer_for_id", k))  # fmt: skip
            db.execute(
                "INSERT INTO push_cursor(log, seq, head) VALUES (?,?,?) "
                "ON CONFLICT(log) DO UPDATE SET seq=excluded.seq, head=excluded.head",
                (log, seq, head),
            )
            return n

        with self._lock:
            return int(_atomic(self._db, body))

    def reset_cursor(self, log: str) -> None:
        with self._lock:
            _atomic(self._db, lambda db: db.execute("DELETE FROM push_cursor WHERE log=?", (log,)))

    def open_intents(self, log: str) -> list[Intent]:
        """The intents of ``log`` that are not final and not yet sent (the abandoned-branch check looks at them);
        ``sent`` ones stay sent."""
        with self._lock:
            rows = self._db.execute(
                f"SELECT {_COLS} FROM push_intent WHERE log=? AND state IN ('planned','sealed') ORDER BY n", (log,)
            ).fetchall()
            return [_intent(r) for r in rows]

    # -- reading -----------------------------------------------------------------------------------------------------

    def get(self, key: str) -> Intent | None:
        with self._lock:
            row = self._db.execute(f"SELECT {_COLS} FROM push_intent WHERE key=?", (key,)).fetchone()
            return None if row is None else _intent(row)

    def planned(self, limit: int = 32) -> list[Intent]:
        with self._lock:
            rows = self._db.execute(
                f"SELECT {_COLS} FROM push_intent WHERE state='planned' ORDER BY n LIMIT ?", (limit,)
            ).fetchall()
            return [_intent(r) for r in rows]

    def sealed(self) -> list[Intent]:
        """Every ``sealed`` intent, oldest first (the push thread picks the first one whose ``next_ms`` is due)."""
        with self._lock:
            rows = self._db.execute(f"SELECT {_COLS} FROM push_intent WHERE state='sealed' ORDER BY n").fetchall()
            return [_intent(r) for r in rows]

    def counts(self) -> dict[str, int]:
        with self._lock:
            return {s: n for s, n in self._db.execute("SELECT state, COUNT(*) FROM push_intent GROUP BY state")}

    def sent_since(self, since_ms: int) -> list[tuple[str, str, int, tuple[str, ...]]]:
        """``(id, kind, sent_ms, targets)`` of the intents sent at or after ``since_ms`` (host budgets)."""
        with self._lock:
            rows = self._db.execute(
                "SELECT id, kind, sent_ms, targets FROM push_intent WHERE sent_ms IS NOT NULL AND sent_ms>=?",
                (since_ms,),
            ).fetchall()
            return [(r[0], r[1], r[2], tuple(json.loads(r[3] or "[]"))) for r in rows]

    def ts_last(self, id_: str) -> int | None:
        with self._lock:
            row = self._db.execute("SELECT last_ts FROM push_ts WHERE id_hash=?", (id_hash(id_),)).fetchone()
            return None if row is None else row[0]

    # -- transitions -------------------------------------------------------------------------------------------------

    def _set(self, key: str, sets: str, args: tuple[Any, ...], *, only: tuple[str, ...]) -> bool:
        def body(db: sqlite3.Connection) -> bool:
            cur = db.execute(
                f"UPDATE push_intent SET {sets} WHERE key=? AND state IN ({','.join('?' * len(only))})",
                (*args, key, *only),
            )
            return bool(cur.rowcount)

        with self._lock:
            return bool(_atomic(self._db, body))

    def seal(self, sealed: Iterable[tuple[str, bytes, int, int, Sequence[str], int]], now_ms: int) -> None:
        """ONE transaction: each ``(key, outer, epoch, ts_ms, targets, auth_gen)`` becomes ``sealed`` with its outer,
        and ``push_ts`` takes ``ts_ms`` (rows older than 25 h are pruned). Called inside the seal span, before any
        send (D6)."""
        items = list(sealed)

        def body(db: sqlite3.Connection) -> None:
            for key, outer, epoch, ts_ms, targets, auth_gen in items:
                row = db.execute("SELECT id FROM push_intent WHERE key=? AND state='planned'", (key,)).fetchone()
                if row is None:
                    continue
                db.execute(
                    "UPDATE push_intent SET state='sealed', outer=?, epoch=?, ts_ms=?, targets=?, auth_gen=?, "
                    "sealed_ms=?, next_ms=0, reason=NULL WHERE key=?",
                    (outer, epoch, ts_ms, json.dumps(list(targets)), auth_gen, now_ms, key),
                )
                db.execute(
                    "INSERT INTO push_ts(id_hash, last_ts, seen_ms) VALUES (?,?,?) "
                    "ON CONFLICT(id_hash) DO UPDATE SET last_ts=MAX(last_ts, excluded.last_ts), "
                    "seen_ms=excluded.seen_ms",
                    (id_hash(row[0]), ts_ms, now_ms),
                )
            db.execute("DELETE FROM push_ts WHERE seen_ms<?", (now_ms - PUSH_TS_KEEP_MS,))

        with self._lock:
            _atomic(self._db, body)

    def record_targets(self, key: str, targets: Sequence[str], auth_gen: int) -> bool:
        """The targets decided at this dispatch (every attempt records them)."""
        return self._set(key, "targets=?, auth_gen=?", (json.dumps(list(targets)), auth_gen), only=("sealed",))

    def drop(self, key: str, reason: str) -> bool:
        return self._set(key, "state='dropped', reason=?, label=''", (reason,), only=("planned", "sealed"))

    def fail(self, key: str, reason: str) -> bool:
        return self._set(key, "state='failed', reason=?, label=''", (reason,), only=("planned", "sealed"))

    def sent(self, key: str, accepted: int, now_ms: int) -> bool:
        return self._set(
            key, "state='sent', accepted=?, sent_ms=?, reason=NULL, label=''", (accepted, now_ms), only=("sealed",)
        )

    def retry(self, key: str, next_ms: int, reason: str) -> bool:
        """The same outer again later: ``attempts + 1``, nothing else changes."""
        return self._set(key, "attempts=attempts+1, next_ms=?, reason=?", (next_ms, reason), only=("sealed",))

    def reseal(self, key: str, reason: str) -> int | None:
        """409 ``stale_epoch``: back to ``planned`` (the outer, epoch and ts are dropped; the deadline is kept);
        returns the new ``reseals`` count, ``None`` when the intent is not ``sealed``."""

        def body(db: sqlite3.Connection) -> int | None:
            cur = db.execute(
                "UPDATE push_intent SET state='planned', outer=NULL, epoch=NULL, ts_ms=NULL, targets=NULL, "
                "next_ms=0, reseals=reseals+1, reason=? WHERE key=? AND state='sealed'",
                (reason, key),
            )
            if not cur.rowcount:
                return None
            return int(db.execute("SELECT reseals FROM push_intent WHERE key=?", (key,)).fetchone()[0])

        with self._lock:
            return _atomic(self._db, body)  # type: ignore[no-any-return]

    def drop_open(self, reason: str) -> int:
        """Every ``planned`` and ``sealed`` intent becomes ``dropped`` (404: push is off at this relay)."""

        def body(db: sqlite3.Connection) -> int:
            return db.execute(
                "UPDATE push_intent SET state='dropped', reason=?, label='' WHERE state IN ('planned','sealed')",
                (reason,),
            ).rowcount

        with self._lock:
            return int(_atomic(self._db, body))

    def release_waiting(self, reason: str) -> int:
        """Sealed intents delayed with ``reason`` (404: off at the relay) become due now."""

        def body(db: sqlite3.Connection) -> int:
            return db.execute("UPDATE push_intent SET next_ms=0 WHERE state='sealed' AND reason=?", (reason,)).rowcount

        with self._lock:
            return int(_atomic(self._db, body))

    def prune(self, before_ms: int) -> int:
        """Final rows older than ``before_ms`` (by deadline) go; the cursor and ``push_ts`` stay."""

        def body(db: sqlite3.Connection) -> int:
            return db.execute(
                f"DELETE FROM push_intent WHERE state IN ({','.join('?' * len(FINAL))}) AND deadline_ms<?",
                (*FINAL, before_ms),
            ).rowcount

        with self._lock:
            return int(_atomic(self._db, body))
