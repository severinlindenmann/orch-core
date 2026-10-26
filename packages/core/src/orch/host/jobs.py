"""Job completion records (§0.7 "job completion", §0.8.2): ``<state>/host/jobs/<id>`` (0600).

H2b owns the repeat rule of ``job.done``; H10 adds the worker, the leases and the containment around it. The host
writes one record per job when it hands the job out: ``{id, workspace_id, kind, lease, state, completion?}``. A repeat
of ``job.done`` is compared with the persisted record, so it answers ``{}`` from any worker connection and across a
host restart; there is no ``opts.idem`` and no outcome record.
"""

from __future__ import annotations

import contextlib
import hashlib
import os
import re
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from orch import canon

from .custody import ensure_private_dir, read_json, write_private
from .errors import HostError

__all__ = ["KEEP_SECONDS", "CompletionStore", "completion_digest"]

KEEP_SECONDS = 24 * 3600
_ULID = re.compile(r"[0-7][0-9A-HJKMNP-TV-Z]{25}")


def completion_digest(outcome: str, result: Any = None, error: Any = None) -> str:
    """``sha256(cj({outcome, result}))`` for ``ok``, ``sha256(cj({outcome, error:{code, message}}))`` for ``error``."""
    if outcome == "ok":
        doc: dict[str, Any] = {"outcome": outcome, "result": result}
    else:
        err = error or {}
        doc = {"outcome": outcome, "error": {"code": err.get("code"), "message": err.get("message")}}
    return "sha256:" + hashlib.sha256(canon.cj_checked(doc)).hexdigest()


class CompletionStore:
    def __init__(self, state_dir: Path, host_uid: int, *, clock: Callable[[], float] = time.time) -> None:
        self.dir = Path(state_dir) / "host" / "jobs"
        self.host_uid = host_uid
        self.clock = clock
        self._lock = threading.Lock()
        #: job id -> the connection its lease is bound to (memory only: a restart voids every lease)
        self._bound: dict[str, object] = {}

    def open(self) -> None:
        """At host start every ``leased`` record becomes ``void``: no connection survives a restart."""
        ensure_private_dir(self.dir.parent, self.host_uid)
        ensure_private_dir(self.dir, self.host_uid)
        for name in os.listdir(self.dir):
            record = read_json(self.dir / name, self.host_uid) if _ULID.fullmatch(name) else None
            if record is not None and record.get("state") == "leased":
                write_private(self.dir / name, {**record, "state": "void", "void_at": int(self.clock())})

    def _path(self, job: str) -> Path:
        if not _ULID.fullmatch(job):
            raise HostError("worker.lease", "unknown job")
        return self.dir / job

    def lease(self, job: str, workspace_id: str, kind: str, lease: str, connection: object) -> None:
        """Persist a job handed out under ``lease`` on ``connection`` (a retry under the same id gets a new lease)."""
        with self._lock:
            write_private(
                self._path(job),
                {
                    "id": job,
                    "workspace_id": workspace_id,
                    "kind": kind,
                    "lease": lease,
                    "state": "leased",
                    "at": int(self.clock()),
                },
            )
            self._bound[job] = connection

    def void(self, connection: object) -> list[str]:
        """The worker connection dropped: its ``leased`` records become ``void``. Returns the job ids."""
        out = []
        with self._lock:
            for job, bound in list(self._bound.items()):
                if bound is connection:
                    del self._bound[job]
                    record = read_json(self._path(job), self.host_uid)
                    if record is not None and record["state"] == "leased":
                        write_private(self._path(job), {**record, "state": "void", "void_at": int(self.clock())})
                        out.append(job)
        return out

    def done(self, job: str, lease: str, outcome: str, digest: str, connection: object) -> dict[str, Any]:
        """``job.done``: the answer ``{}`` or ``worker.lease`` (§0.8.2 precedence 1 to 4)."""
        with self._lock:
            record = read_json(self._path(job), self.host_uid)
            if record is None:
                raise HostError("worker.lease", "unknown job")
            state = record["state"]
            if state == "done":
                if record["lease"] == lease and record["completion"]["digest"] == digest:
                    return {}
                raise HostError("worker.lease", "this job is already complete")
            if state == "leased" and record["lease"] == lease and self._bound.get(job) is connection:
                write_private(
                    self._path(job),
                    {
                        **record,
                        "state": "done",
                        "completion": {"outcome": outcome, "digest": digest},
                        "done_at": int(self.clock()),
                    },
                )
                self._bound.pop(job, None)
                return {}
            raise HostError("worker.lease", "the lease is not valid")

    def sweep(self) -> None:
        """Records are kept 24 h after completion; ``leased`` and ``void`` ones 24 h after they were handed out or
        voided. Blocking file work: the host calls it off the event loop (``Host.sweep_once``)."""
        now = self.clock()
        for name in os.listdir(self.dir):
            record = read_json(self.dir / name, self.host_uid) if _ULID.fullmatch(name) else None
            if record is None:
                continue
            since = record.get("done_at") or record.get("void_at") or record.get("at")
            if (
                since is not None
                and now - since > KEEP_SECONDS
                and name not in self._bound
                and record.get("state") in {"done", "void", "leased"}
            ):
                with contextlib.suppress(FileNotFoundError):  # another host's sweep or a manual cleanup won
                    os.unlink(self.dir / name)
