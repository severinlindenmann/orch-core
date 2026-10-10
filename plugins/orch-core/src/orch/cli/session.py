"""Per-session records the CLI needs between two calls: the stop rule and retry dedup.

``ORCH_SESSION`` is an identifier an agent asserts about itself; it is **not an authenticator** (session ids are
public: every agent event carries one). Nothing here may widen what a caller can see or do, so dedup keys also bind
the grant id and the mode, and a cached result is only returned after the grant check ran again.

* **Stop rule** (format 10.4 item 6) is *advisory*: it helps an agent stop looping, it is not a control (an agent
  can drop or change ``ORCH_SESSION``). The same refusal is the same operation, the same normalised arguments
  (``REF`` as the ticket key) and the same error code, three times within 15 minutes; ``human_only`` counts by
  operation and code, ignoring arguments. ``stop`` then answers the identical call without running it, until
  15 minutes after the last refusal. Only a successful write resets the count; reads, usage errors, retryable
  and internal errors neither count nor reset.
* **Retry dedup** (10.4 item 9): the same session, grant id, mode (attended or not), operation and arguments, and
  no event on the ticket since (its head ``seq`` is part of the key), within 15 minutes, returns the original
  result with ``duplicate: true``. Writes only, never with ``--dry-run``.

:class:`MemoryRecords` lives as long as the process; :class:`FileRecords` keeps one JSON file per session.

TODO (C6, acceptance criteria for wiring ``FileRecords`` to the workspace ``.state/``): the dedup record must be
written in the same lock or transaction as the ``Store.append`` it describes (a crash between the two makes the
retry append twice), or dedup must be derived from the log; read-modify-write needs a file lock so two processes of
one session lose no update; a corrupt or partial file must fail closed, not reset the stop rule, and a file with
missing keys must not raise. ``tests/cli/test_errors.py`` has a skipped test describing the two-process case.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from pathlib import Path
from typing import Any

__all__ = ["DEDUP_SECONDS", "STOP_AFTER", "STOP_SECONDS", "FileRecords", "MemoryRecords", "fingerprint"]

DEDUP_SECONDS = 15 * 60
STOP_SECONDS = 15 * 60
STOP_AFTER = 3


def fingerprint(*parts: Any) -> str:
    """A stable id of any JSON-able parts."""
    raw = json.dumps(list(parts), sort_keys=True, ensure_ascii=False, separators=(",", ":"), default=str)
    return hashlib.sha256(raw.encode()).hexdigest()


def _empty() -> dict[str, Any]:
    return {"refusals": {}, "stops": {}, "dedup": {}}


class MemoryRecords:
    def __init__(self) -> None:
        self._state: dict[str, dict[str, Any]] = {}

    def _get(self, session: str) -> dict[str, Any]:
        st = self._state.setdefault(session, _empty())
        for k, v in _empty().items():
            st.setdefault(k, v)
        return st

    def _save(self, session: str, state: dict[str, Any]) -> None:
        self._state[session] = state

    # -- stop rule
    @staticmethod
    def _keys(op: str, args: dict[str, Any], code: str) -> tuple[str, str]:
        """(refusal key, stop mark key): human_only counts by operation and code only."""
        a = "*" if code == "human_only" else args
        return fingerprint(op, a, code), fingerprint(op, a)

    def refused(self, session: str, op: str, args: dict[str, Any], code: str, now: float) -> int:
        """Record a refusal; return how many times this same refusal happened in the last 15 minutes."""
        st = self._get(session)
        rkey, mark = self._keys(op, args, code)
        times = [t for t in st["refusals"].get(rkey, []) if now - t <= STOP_SECONDS] + [now]
        st["refusals"][rkey] = times
        if len(times) >= STOP_AFTER:
            st["stops"][mark] = now
        self._save(session, st)
        return len(times)

    def stopped(self, session: str, op: str, args: dict[str, Any], now: float) -> bool:
        """Has this call been refused the same way three times in a row, less than 15 minutes ago?"""
        st = self._get(session)
        for a in (args, "*"):
            ts = st["stops"].get(fingerprint(op, a))
            if ts is not None and now - ts <= STOP_SECONDS:
                return True
        return False

    def succeeded_write(self, session: str) -> None:
        st = self._get(session)
        st["refusals"], st["stops"] = {}, {}
        self._save(session, st)

    # -- retry dedup
    def recall(self, session: str, key: str, now: float) -> dict[str, Any] | None:
        hit = self._get(session)["dedup"].get(key)
        if hit and now - hit["at"] <= DEDUP_SECONDS:
            return hit["result"]
        return None

    def remember(self, session: str, key: str, now: float, result: dict[str, Any]) -> None:
        st = self._get(session)
        st["dedup"] = {k: v for k, v in st["dedup"].items() if now - v["at"] <= DEDUP_SECONDS}
        st["dedup"][key] = {"at": now, "result": result}
        self._save(session, st)


class FileRecords(MemoryRecords):
    """The same records, one ``<session>.json`` per session under ``directory``. Not safe for two writers yet
    (see the TODO in the module docstring)."""

    def __init__(self, directory: str | os.PathLike[str]) -> None:
        super().__init__()
        self.directory = Path(directory)

    def _path(self, session: str) -> Path:
        return self.directory / (session.replace("/", "_") + ".json")

    def _get(self, session: str) -> dict[str, Any]:
        try:
            st = json.loads(self._path(session).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            st = _empty()
        for k, v in _empty().items():
            st.setdefault(k, v)
        return st

    def _save(self, session: str, state: dict[str, Any]) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self.directory, suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(state, fh)
        os.replace(tmp, self._path(session))
