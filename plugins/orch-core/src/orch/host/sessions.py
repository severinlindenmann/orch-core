"""Host session records (H6b, §0.8 ``session.list``, §0.8.5 ``grant.list`` ``session_ids``).

F1 stores no session list: a session is a public label an agent puts in ``auth.session``. The host therefore keeps its
own record for every column-G request that carries one: ``{session, agent, person, grant_id, last_at}``, keyed by
(workspace, grant id, session). They are **host records**, not signed and in no log (§0.8.5); the dashboard reads them
through ``session.list`` and the ``session_ids`` of ``grant.list``, each under the ``grant.list`` person rule.

* **Where.** ``<state>/hosts/<ws>/sessions.json`` (0600, atomic replace, no fsync), read once per workspace service,
  then kept in memory. The list the reads serve is the in-memory one, so ``last_at`` is exact; the file is written
  only when a session is new or its ``last_at`` moved by :data:`WRITE_EVERY` (60 s) since the last write, so a busy
  session costs one small write a minute.
* **Bounds.** At most :data:`MAX_RECORDS` records (the oldest ``last_at`` is dropped first), none older than
  :data:`RETENTION` (7 days).
* **The agent id** is the one the host stamps on that session's events: ``orch.ops.runtime.Call.agent_id()`` with the
  host's capabilities (an empty environment), so ``"orch-cli"``.
* :func:`record` is called by the dispatcher for every request it serves, after the column. It never raises into
  dispatch: a failure is logged and the request goes on.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .workspace import Call, WorkspaceService

__all__ = ["MAX_RECORDS", "RETENTION", "WRITE_EVERY", "Record", "Records", "agent_id", "records", "record"]

LOG = logging.getLogger(__name__)
MAX_RECORDS = 10_000
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
    """The session records of one workspace. Worker thread only (a lock guards the file against a stray reader)."""

    def __init__(self, path: Path, host_uid: int) -> None:
        self.path = path
        self.host_uid = host_uid
        self._items: dict[tuple[str, str], Record] = {}
        self._lock = threading.Lock()
        self._loaded = False

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
                self._items[(rec.grant_id, rec.session)] = rec

    def _flush(self) -> None:
        import json

        from orch.store.fsio import write_atomic

        from .custody import ensure_private_dir

        ensure_private_dir(self.path.parent, self.host_uid, create=False)
        rows = [
            {"session": r.session, "agent": r.agent, "person": r.person, "grant_id": r.grant_id, "last_at": r.last_at}
            for r in sorted(self._items.values(), key=lambda r: (r.last_at, r.grant_id, r.session))
        ]
        doc = {"v": _VERSION, "sessions": rows}
        # atomic (a reader sees the old file or the new one) but not fsynced: these are display records, and the
        # dispatcher must not wait for the disk
        write_atomic(
            self.path, json.dumps(doc, sort_keys=True, separators=(",", ":")).encode(), mode=0o600, durable=False
        )
        for r in self._items.values():
            r.written = r.last_at

    def _prune(self, now: float) -> None:
        for key in [k for k, r in self._items.items() if r.last_at <= now - RETENTION]:
            del self._items[key]
        if len(self._items) > MAX_RECORDS:
            keep = sorted(self._items.items(), key=lambda kv: (kv[1].last_at, kv[0]), reverse=True)[:MAX_RECORDS]
            self._items = dict(keep)

    # -- use
    def note(self, session: str, agent: str, person: str, grant_id: str, now: float) -> None:
        """One request of ``session`` under ``grant_id`` at ``now``."""
        with self._lock:
            if not self._loaded:
                self._load(now)
            at = int(now)
            key = (grant_id, session)
            rec = self._items.get(key)
            if rec is None:
                rec = self._items[key] = Record(session, agent, person, grant_id, at)
                fresh = True
            else:
                fresh = rec.written is None or at - rec.written >= WRITE_EVERY
                rec.last_at = max(rec.last_at, at)
                rec.agent, rec.person = agent, person
            if fresh:
                self._prune(now)
                try:
                    self._flush()
                except Exception as exc:  # noqa: BLE001 - the file is a convenience; memory still serves
                    LOG.warning("session records %s not written: %s", self.path, exc)

    def snapshot(self, now: float) -> list[Record]:
        """Every record within the retention, as copies (one consistent snapshot for one read)."""
        with self._lock:
            if not self._loaded:
                self._load(now)
            return [
                Record(r.session, r.agent, r.person, r.grant_id, r.last_at, r.written)
                for r in self._items.values()
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
