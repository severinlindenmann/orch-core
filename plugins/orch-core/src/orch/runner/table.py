"""The runner's persisted run table (D-P2-39): ``<state>/runner/runs.json``, 0600 in a 0700 directory.

One record per run id, written **before** the spawn (state ``spawning``) and again once the process exists (its
birth identity: pid, birth time, process group, session, cgroup). The table is what makes three things possible:

* spawn dedupe: a ``spawn`` whose run id is already in the table never starts a second process (a lost ack, a host
  reconnect, a host restart between intent and ack);
* the crash sweep: after a runner crash every run still marked live becomes ``lost`` with ``cleanup: "unknown"``, and
  the recorded processes that still match their birth identity are stopped;
* honest reports: a reused PID never matches a recorded birth time, so it is never signalled.

The table never holds a secret, an environment value or terminal bytes. Ended runs are kept 7 days, at most 1 000.
"""

from __future__ import annotations

import re
import threading
import time
from pathlib import Path
from typing import Any

from orch.host.custody import ensure_private_dir, read_json, write_private
from orch.host.errors import HostError

__all__ = ["KEEP_ENDED", "KEEP_SECONDS", "RUN_ID", "RunTable", "now_iso"]

RUN_ID = re.compile(r"r_[0-7][0-9A-HJKMNP-TV-Z]{25}")
KEEP_ENDED = 1000
KEEP_SECONDS = 7 * 24 * 3600
LIVE = ("spawning", "running")


def now_iso(clock: float | None = None) -> str:
    t = time.time() if clock is None else clock
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(t)) + f".{int((t % 1) * 1000):03d}Z"


class RunTable:
    def __init__(self, directory: Path, uid: int) -> None:
        self.directory = Path(directory)
        self.path = self.directory / "runs.json"
        self.uid = uid
        self.lock = threading.RLock()
        self.runs: dict[str, dict[str, Any]] = {}

    def load(self) -> None:
        with self.lock:
            ensure_private_dir(self.directory, self.uid)
            doc = read_json(self.path, self.uid)
            if doc is None:
                self.runs = {}
                return
            if not isinstance(doc, dict) or doc.get("v") != 1 or not isinstance(doc.get("runs"), dict):
                raise HostError("internal", f"{self.path}: unreadable run table")
            self.runs = {k: v for k, v in doc["runs"].items() if RUN_ID.fullmatch(k) and isinstance(v, dict)}

    def save(self) -> None:
        with self.lock:
            self._prune()
            write_private(self.path, {"v": 1, "runs": self.runs})

    def _prune(self) -> None:
        # a run whose processes may still be there (cleanup not known complete) is never pruned: it stays supervised
        ended = [
            (r.get("ended_ts") or 0, k)
            for k, r in self.runs.items()
            if r.get("state") not in LIVE and (r.get("cleanup") == "complete" or not isinstance(r.get("pid"), int))
        ]
        cutoff = time.time() - KEEP_SECONDS
        ended.sort()
        drop = {k for ts, k in ended if ts < cutoff}
        excess = len(ended) - len(drop) - KEEP_ENDED
        if excess > 0:
            drop |= {k for _, k in [e for e in ended if e[1] not in drop][:excess]}
        for k in drop:
            del self.runs[k]

    def get(self, run: str) -> dict[str, Any] | None:
        with self.lock:
            rec = self.runs.get(run)
            return None if rec is None else dict(rec)

    def put(self, run: str, record: dict[str, Any]) -> None:
        if not RUN_ID.fullmatch(run):
            raise ValueError("invalid run id")
        with self.lock:
            self.runs[run] = dict(record)
            if record.get("state") not in LIVE:
                self.runs[run].setdefault("ended_ts", time.time())
            self.save()

    def update(self, run: str, **fields: Any) -> dict[str, Any]:
        with self.lock:
            rec = self.runs[run]
            rec.update(fields)
            if fields.get("state") not in (None, *LIVE) and "ended_ts" not in rec:
                rec["ended_ts"] = time.time()
            self.save()
            return dict(rec)

    def live(self) -> list[tuple[str, dict[str, Any]]]:
        with self.lock:
            return [(k, dict(v)) for k, v in self.runs.items() if v.get("state") in LIVE]

    def all(self) -> list[tuple[str, dict[str, Any]]]:
        with self.lock:
            return [(k, dict(v)) for k, v in self.runs.items()]
