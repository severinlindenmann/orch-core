"""Private, hub-local administrative history. This is not the hub.audit contract."""

from __future__ import annotations

import hashlib
import json
import threading
import time

from ..fsutil import open_private_db

ACTIVITY_TYPES = (
    "invite.created",
    "invite.redeemed",
    "reset.issued",
    "recovery.redeemed",
    "user.disabled",
    "user.enabled",
    "authenticator.rotated",
    "password.changed",
    "sessions.ended",
    "change.proposed",
    "change.approved",
    "change.overridden",
    "change.expired",
    "change.rejected",
    "connection.allowed",
    "connection.denied",
    "connection.migrated",
    "channel.revoked",
    "pairing.issued",
    "pairing.used",
)
# Only structured references, never request bodies, credentials, or free-form notes.
DETAIL_FIELDS = frozenset(
    {
        "channel_id",
        "change_id",
        "connection_id",
        "invite_id",
        "pairing_id",
        "requested_by_agent_session",
        "approved_by",
        "count",
    }
)
FIELDS = [
    "seq",
    "ts_ms",
    "prev",
    "hash",
    "type",
    "actor_kind",
    "actor_id",
    "actor_session",
    "subject_user",
    "workspace_id",
    "detail",
]


def canonical(row):
    return json.dumps(row, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode("utf-8")


def bounded(value):
    if value is not None and (
        not isinstance(value, str)
        or not value
        or len(value.encode("utf-8")) > 256
        or any(ord(c) < 32 or ord(c) == 127 for c in value)
    ):
        raise ValueError("invalid activity reference")
    return value


def digest(row):
    return hashlib.sha256(
        row["prev"].encode() + canonical({k: v for k, v in row.items() if k not in ("prev", "hash")})
    ).hexdigest()


class ActivityLog:
    def __init__(self, path=":memory:", *, now_ms=None):
        self.lock = threading.RLock()
        self._now = now_ms or (lambda: int(time.time() * 1000))
        self._db = open_private_db(str(path), check_same_thread=False)
        self._db.execute("""CREATE TABLE IF NOT EXISTS event (
            seq INTEGER PRIMARY KEY, ts_ms INTEGER NOT NULL, prev TEXT NOT NULL, hash TEXT NOT NULL,
            type TEXT NOT NULL, actor_kind TEXT NOT NULL, actor_id TEXT, actor_session TEXT,
            subject_user TEXT, workspace_id TEXT, detail TEXT NOT NULL)""")
        self._db.commit()

    def close(self):
        with self.lock:
            self._db.close()

    def record(
        self,
        type,
        *,
        actor_kind="person",
        actor_id=None,
        actor_session=None,
        subject_user=None,
        workspace_id=None,
        detail=None,
    ):
        if type not in ACTIVITY_TYPES or actor_kind not in ("person", "agent", "host", "hub"):
            raise ValueError("unknown activity type or actor")
        for value in (actor_id, actor_session, subject_user, workspace_id):
            bounded(value)
        if actor_kind == "agent" and (not actor_session or actor_id is not None):
            raise ValueError("agents use session references only")
        detail = {} if detail is None else detail
        if not isinstance(detail, dict) or detail.keys() - DETAIL_FIELDS:
            raise ValueError("activity detail accepts references only")
        for key, value in detail.items():
            if key == "count":
                if not isinstance(value, int) or isinstance(value, bool) or not 0 <= value < 2**63:
                    raise ValueError("invalid count")
            else:
                bounded(value)
        encoded = canonical(detail).decode()
        if len(encoded.encode()) > 2048:
            raise ValueError("activity detail too large")
        # BEGIN IMMEDIATE also serializes writers using other connections/processes.
        with self.lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                head = self._db.execute("SELECT seq, ts_ms, hash FROM event ORDER BY seq DESC LIMIT 1").fetchone()
                row = {
                    "seq": head[0] + 1 if head else 1,
                    "ts_ms": max(self._now(), head[1] if head else 0),
                    "prev": head[2] if head else "0" * 64,
                    "type": type,
                    "actor_kind": actor_kind,
                    "actor_id": actor_id,
                    "actor_session": actor_session,
                    "subject_user": subject_user,
                    "workspace_id": workspace_id,
                    "detail": json.loads(encoded),
                }
                row["hash"] = digest(row)
                self._db.execute(
                    "INSERT INTO event VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    [encoded if k == "detail" else row[k] for k in FIELDS],
                )
                self._db.commit()
                return row["seq"]
            except BaseException:
                self._db.rollback()
                raise

    def list(self, before_seq=None, limit=100, type=None, workspace_id=None, person=None, since_ms=None, until_ms=None):
        if not isinstance(limit, int) or not 1 <= limit <= 200:
            raise ValueError("invalid limit")
        where, args = [], []
        for column, op, value in (
            ("seq", "<", before_seq),
            ("type", "=", type),
            ("workspace_id", "=", workspace_id),
            ("ts_ms", ">=", since_ms),
            ("ts_ms", "<=", until_ms),
        ):
            if value is not None:
                where.append(f"{column} {op} ?")
                args.append(value)
        if person is not None:
            where.append(
                "((actor_kind='person' AND actor_id=?) OR subject_user=? OR json_extract(detail, '$.approved_by')=?)"
            )
            args.extend([person] * 3)
        with self.lock:
            rows = self._db.execute(
                "SELECT * FROM event"
                + (" WHERE " + " AND ".join(where) if where else "")
                + " ORDER BY seq DESC LIMIT ?",
                [*args, limit],
            ).fetchall()
        return [dict(zip(FIELDS, [*r[:-1], json.loads(r[-1])])) for r in rows]

    def verify(self):
        with self.lock:
            rows = self._db.execute("SELECT * FROM event ORDER BY seq").fetchall()
        prev = "0" * 64
        last_time = 0
        try:
            for seq, values in enumerate(rows, 1):
                row = dict(zip(FIELDS, [*values[:-1], json.loads(values[-1])]))
                if row["seq"] != seq or row["prev"] != prev or row["hash"] != digest(row) or row["ts_ms"] < last_time:
                    return False
                prev, last_time = row["hash"], row["ts_ms"]
        except ValueError, TypeError:
            return False
        return True
