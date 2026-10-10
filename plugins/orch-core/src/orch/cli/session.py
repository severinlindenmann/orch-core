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

Wiring to a workspace (``orch.cli.store_hooks``): ``FileRecords`` under ``.state/sessions``; the dedup key goes to
``Store.append(idem=...)`` through ``Context.idem``, so a crash between the append and the record cannot make the
retry append twice.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
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
    return {"refusals": {}, "stops": {}, "dedup": {}, "attempts": {}}


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

    def attempt(self, session: str, base_key: str, now: float) -> str:
        """The id of this attempt at an operation (``base_key``: session, grant, mode, operation and arguments, but not
        the ticket head). It is recorded before the handler runs and cleared by :meth:`remember` in the same write, so
        a crash between the append and the record leaves it behind: the retry gets the same id, which the store uses
        as ``Store.append(idem=...)`` and answers with the first append instead of appending again."""
        st = self._get(session)
        st["attempts"] = {k: v for k, v in st["attempts"].items() if now - v["at"] <= DEDUP_SECONDS}
        hit = st["attempts"].get(base_key)
        if hit is None:
            hit = st["attempts"][base_key] = {"id": hashlib.sha256(os.urandom(16)).hexdigest(), "at": now}
            self._save(session, st)
        return fingerprint(base_key, hit["id"])

    def remember(self, session: str, key: str, now: float, result: dict[str, Any], base_key: str | None = None) -> None:
        st = self._get(session)
        if base_key is not None:
            st["attempts"].pop(base_key, None)
        st["dedup"] = {k: v for k, v in st["dedup"].items() if now - v["at"] <= DEDUP_SECONDS}
        st["dedup"][key] = {"at": now, "result": result}
        self._save(session, st)


class FileRecords(MemoryRecords):
    """The same records, one ``<session>.json`` per session under ``directory``.

    * Every operation is one read-modify-write under a file lock (``orch.store.FileLock`` on ``<directory>/.lock``), so
      two processes of one session lose no update.
    * Files are replaced atomically (``orch.store.write_atomic``), never written in place.
    * A file that exists but is not a JSON object **fails closed** (``OrchError`` ``internal``): it is never read as an
      empty record, so corruption cannot reset the stop rule. A file with missing keys is filled with defaults.
    * Retry dedup across a crash is the store's job, not this file's: the CLI hands the dedup key to
      ``Store.append(idem=...)`` (``Context.idem``), which records its intent before the append. A lost record here
      only means the retry runs the handler again, and the store answers it with the first append.
    """

    def __init__(self, directory: str | os.PathLike[str]) -> None:
        super().__init__()
        self.directory = Path(directory)
        from orch.store.lock import FileLock  # lazy: `orch --help` does not need the store

        self._flock = FileLock(self.directory / ".lock")

    def _path(self, session: str) -> Path:
        return self.directory / (re.sub(r"[^A-Za-z0-9_-]", "_", session)[:120] + ".json")

    def _get(self, session: str) -> dict[str, Any]:
        try:
            raw = self._path(session).read_bytes()
        except FileNotFoundError:
            return _empty()
        try:
            st = json.loads(raw)
            if not isinstance(st, dict) or any(not isinstance(st.get(k, {}), dict) for k in _empty()):
                raise ValueError("not a record")
        except ValueError:
            from orch.ops.errors import OrchError

            raise OrchError("internal", f"the session record {self._path(session).name} is corrupt") from None
        for k, v in _empty().items():
            st.setdefault(k, v)
        return st

    def _save(self, session: str, state: dict[str, Any]) -> None:
        from orch.store.fsio import write_atomic

        write_atomic(self._path(session), json.dumps(state).encode("utf-8"), mode=0o600)

    # one lock around each read-modify-write of the base class
    def refused(self, *a: Any, **k: Any) -> int:
        with self._flock:
            return super().refused(*a, **k)

    def stopped(self, *a: Any, **k: Any) -> bool:
        with self._flock:
            return super().stopped(*a, **k)

    def succeeded_write(self, *a: Any, **k: Any) -> None:
        with self._flock:
            return super().succeeded_write(*a, **k)

    def recall(self, *a: Any, **k: Any) -> dict[str, Any] | None:
        with self._flock:
            return super().recall(*a, **k)

    def remember(self, *a: Any, **k: Any) -> None:
        with self._flock:
            return super().remember(*a, **k)

    def attempt(self, *a: Any, **k: Any) -> str:
        with self._flock:
            return super().attempt(*a, **k)

