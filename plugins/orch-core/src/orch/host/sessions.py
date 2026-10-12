"""Host session records (H6b, §0.8 ``session.list``, §0.8.5 ``grant.list`` ``session_ids``).

F1 stores no session list: a session is a public label an agent puts in ``auth.session``. The host therefore keeps its
own record for every column-G request that carries one: ``{session, agent, person, grant_id, last_at}``, keyed by
(workspace, grant id, session). They are **host records**, not signed and in no log (§0.8.5); the dashboard reads them
through ``session.list`` and the ``session_ids`` of ``grant.list``, each under the ``grant.list`` person rule.

* **Where.** ``<state>/hosts/<ws>/sessions.json`` (0600, atomic replace, no fsync), read once per workspace service,
  then kept in memory. The list the reads serve is the in-memory one, so ``last_at`` is exact; the file is written
  at most once per :data:`WRITE_EVERY` (60 s) however many sessions arrive (and when the service closes), so a busy
  host costs one small write a minute.
* **Bounds.** At most :data:`PER_GRANT` records per grant (that grant's oldest is dropped), at most
  :data:`MAX_RECORDS` in all (the grant holding the most loses its oldest first, so one grant cannot evict another's
  records), none older than :data:`RETENTION` (7 days).
* **The agent id** is the one the host stamps on that session's events: ``orch.ops.runtime.Call.agent_id()`` with the
  host's capabilities (an empty environment), so ``"orch-cli"``.
* :func:`record` is called by the dispatcher for every request it serves, after the column. It never raises into
  dispatch: a failure is logged and the request goes on.
"""

from __future__ import annotations

import logging
import threading
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .workspace import Call, WorkspaceService

__all__ = ["MAX_RECORDS", "PER_GRANT", "RETENTION", "WRITE_EVERY", "Record", "Records", "agent_id", "records", "record"]

LOG = logging.getLogger(__name__)
MAX_RECORDS = 10_000
PER_GRANT = 256
RETENTION = 7 * 24 * 3600
WRITE_EVERY = 60
_VERSION = 1


@dataclass
class Record:
    session: str
    agent: str
    person: str
    grant_id: str
    #: epoch seconds of the latest request (exact, in memory)
    last_at: int
    #: the ``last_at`` that is in the file (``None``: never written)
    written: int | None = None


def agent_id(service: WorkspaceService) -> str:
    """The agent id the host stamps on this workspace's agent events: ``Call.agent_id()`` over the host's own
    (empty) environment, never the client's."""
    from orch.ops.runtime import _AGENT_ID

    env = service.capabilities().env
    harness = env.get("ORCH_HARNESS", "")
    return harness if _AGENT_ID.fullmatch(harness) else "orch-cli"


class Records:
    """The session records of one workspace. Worker thread only (a lock guards the file against a stray reader).

    Kept per grant, each grant's records in ``last_at`` order (oldest first), so a drop is O(1) and nothing is sorted
    per request."""

    def __init__(self, path: Path, host_uid: int) -> None:
        self.path = path
        self.host_uid = host_uid
        self._by_grant: dict[str, OrderedDict[str, Record]] = {}
        self._count = 0
        self._lock = threading.Lock()
        self._loaded = False
        self._dirty = False
        self._flushed_at: float | None = None

    # -- the file
    def _load(self, now: float) -> None:
        self._loaded = True
        from .custody import read_json

        try:
            doc = read_json(self.path, self.host_uid)
        except Exception as exc:  # noqa: BLE001 - an unreadable file is an empty list, never a failed request
            LOG.warning("session records %s unreadable: %s", self.path, exc)
            return
        rows = doc.get("sessions") if isinstance(doc, dict) and doc.get("v") == _VERSION else None
        loaded: list[Record] = []
        for row in rows if isinstance(rows, list) else ():
            try:
                rec = Record(
                    str(row["session"]),
                    str(row["agent"]),
                    str(row["person"]),
                    str(row["grant_id"]),
                    int(row["last_at"]),
                )
            except (KeyError, TypeError, ValueError):
                continue
            rec.written = rec.last_at
            if rec.last_at > now - RETENTION:
                loaded.append(rec)
        for rec in sorted(loaded, key=lambda r: r.last_at):  # one sort, at load
            self._put(rec)
        self._trim()

    def flush(self) -> None:
        """Write the file now if anything changed (also called when the workspace service closes)."""
        with self._lock:
            if self._loaded and self._dirty:
                self._dirty = False
                self._flush()

    def _flush(self) -> None:
        import json

        from orch.store.fsio import write_atomic

        from .custody import ensure_private_dir

        try:
            ensure_private_dir(self.path.parent, self.host_uid, create=False)
            recs = [r for grant in self._by_grant.values() for r in grant.values()]
            rows = [
                {
                    "session": r.session,
                    "agent": r.agent,
                    "person": r.person,
                    "grant_id": r.grant_id,
                    "last_at": r.last_at,
                }
                for r in sorted(recs, key=lambda r: (r.last_at, r.grant_id, r.session))
            ]
            doc = {"v": _VERSION, "sessions": rows}
            # atomic (a reader sees the old file or the new one) but not fsynced: these are display records, and the
            # dispatcher must not wait for the disk
            write_atomic(
                self.path, json.dumps(doc, sort_keys=True, separators=(",", ":")).encode(), mode=0o600, durable=False
            )
            for r in recs:
                r.written = r.last_at
        except Exception as exc:  # noqa: BLE001 - the file is a convenience; memory still serves
            LOG.warning("session records %s not written: %s", self.path, exc)

    # -- the in-memory set
    def _put(self, rec: Record) -> None:
        grant = self._by_grant.setdefault(rec.grant_id, OrderedDict())
        if rec.session not in grant:
            self._count += 1
        grant[rec.session] = rec
        grant.move_to_end(rec.session)

    def _drop_oldest(self, grant_id: str) -> None:
        grant = self._by_grant[grant_id]
        grant.popitem(last=False)
        self._count -= 1
        if not grant:
            del self._by_grant[grant_id]

    def _trim(self) -> None:
        """Per-grant cap first, then the workspace cap: the grant holding the most records loses its oldest, so one
        grant (a client choosing a fresh session id per request) cannot push out another grant's records."""
        for gid in [g for g, d in self._by_grant.items() if len(d) > PER_GRANT]:
            while gid in self._by_grant and len(self._by_grant[gid]) > PER_GRANT:
                self._drop_oldest(gid)
        while self._count > MAX_RECORDS:
            self._drop_oldest(max(self._by_grant, key=lambda g: len(self._by_grant[g])))

    def _expire(self, now: float) -> None:
        for gid in list(self._by_grant):
            grant = self._by_grant[gid]
            while grant and next(iter(grant.values())).last_at <= now - RETENTION:
                self._drop_oldest(gid)

    # -- use
    def note(self, session: str, agent: str, person: str, grant_id: str, now: float) -> None:
        """One request of ``session`` under ``grant_id`` at ``now``. The file is rewritten at most once per
        :data:`WRITE_EVERY` seconds, whatever arrived meanwhile."""
        with self._lock:
            if not self._loaded:
                self._load(now)
            at = int(now)
            rec = self._by_grant.get(grant_id, {}).get(session)
            if rec is None:
                self._put(Record(session, agent, person, grant_id, at))
                self._trim()
            else:
                rec.last_at = max(rec.last_at, at)
                rec.agent, rec.person = agent, person
                self._by_grant[grant_id].move_to_end(session)
            self._dirty = True
            if self._flushed_at is None or now - self._flushed_at >= WRITE_EVERY:
                self._expire(now)
                self._dirty, self._flushed_at = False, now
                self._flush()

    def snapshot(self, now: float) -> list[Record]:
        """Every record within the retention, as copies (one consistent snapshot for one read)."""
        with self._lock:
            if not self._loaded:
                self._load(now)
            return [
                Record(r.session, r.agent, r.person, r.grant_id, r.last_at, r.written)
                for grant in self._by_grant.values()
                for r in grant.values()
                if r.last_at > now - RETENTION
            ]


def records(service: WorkspaceService) -> Records:
    """The records of ``service`` (created on first use)."""
    got = getattr(service, "session_records", None)
    if got is None:
        path = Path(service.state_dir) / "hosts" / service.workspace_id / "sessions.json"
        got = service.session_records = Records(path, service.host_uid)
    return got


def record(call: Call) -> None:
    """Note the (grant, session) of a column-G request with ``auth.session``. Never raises into dispatch."""
    try:
        if call.column.name != "G":
            return
        session = (call.frame.get("auth") or {}).get("session")
        grant: Any = call.verified.grant
        if not isinstance(session, str) or grant is None or call.viewer is None:
            return
        from orch.cli.main import _SESSION

        if not _SESSION.search(session):  # the id an agent's events carry (s_<ULID>, then at most three .n)
            return
        service = call.service
        records(service).note(session, agent_id(service), call.viewer, grant.id, service.clock())
    except Exception as exc:  # noqa: BLE001 - bookkeeping never changes an op's answer
        LOG.warning("session record failed: %s", exc)
