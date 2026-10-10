"""Per-session records the CLI needs between two calls: the stop rule and retry dedup.

Both are kept per session (``ORCH_SESSION``) and are not events (format 5.4: "Not events. Refusals (human_only,
stop rule) are kept in the session records in .state/"). :class:`MemoryRecords` lives as long as the process;
:class:`FileRecords` keeps one JSON file per session in a directory the caller names (the workspace's
``.state/session/``), written atomically.

* **Stop rule** (10.4 item 6): the same refusal three times in a row in one session. "The same" is the same
  operation, the same error code and the same arguments. Any success, any other refusal or any other call resets
  the count.
* **Retry dedup** (10.4 item 9): the same session, operation and arguments within 15 minutes return the original
  result with ``duplicate: true``.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from pathlib import Path
from typing import Any

__all__ = ["DEDUP_SECONDS", "STOP_AFTER", "FileRecords", "MemoryRecords", "fingerprint"]

DEDUP_SECONDS = 15 * 60
STOP_AFTER = 3


def fingerprint(op: str, args: dict[str, Any], code: str | None = None) -> str:
    """A stable id of a call (operation and arguments), or of a refusal when ``code`` is given."""
    raw = json.dumps([op, code, args], sort_keys=True, ensure_ascii=False, separators=(",", ":"), default=str)
    return hashlib.sha256(raw.encode()).hexdigest()


class MemoryRecords:
    def __init__(self) -> None:
        self._state: dict[str, dict[str, Any]] = {}

    def _get(self, session: str) -> dict[str, Any]:
        return self._state.setdefault(session, {"refusal": None, "count": 0, "dedup": {}})

    def _save(self, session: str, state: dict[str, Any]) -> None:
        self._state[session] = state

    # -- stop rule
    def refused(self, session: str, op: str, args: dict[str, Any], code: str) -> int:
        """Record a refusal; return how many times in a row this same refusal has now happened."""
        st = self._get(session)
        fp = fingerprint(op, args, code)
        st["count"] = st["count"] + 1 if st["refusal"] == fp else 1
        st["refusal"] = fp
        st["call"] = fingerprint(op, args)
        self._save(session, st)
        return st["count"]

    def stopped(self, session: str, op: str, args: dict[str, Any]) -> bool:
        """Has this very call already been refused the same way three times in a row?"""
        st = self._get(session)
        return st.get("call") == fingerprint(op, args) and st["count"] >= STOP_AFTER

    def succeeded(self, session: str) -> None:
        st = self._get(session)
        st["refusal"], st["call"], st["count"] = None, None, 0
        self._save(session, st)

    # -- retry dedup
    def recall(self, session: str, op: str, args: dict[str, Any], now: float) -> dict[str, Any] | None:
        st = self._get(session)
        hit = st["dedup"].get(fingerprint(op, args))
        if hit and now - hit["at"] <= DEDUP_SECONDS:
            return hit["result"]
        return None

    def remember(self, session: str, op: str, args: dict[str, Any], now: float, result: dict[str, Any]) -> None:
        st = self._get(session)
        st["dedup"] = {k: v for k, v in st["dedup"].items() if now - v["at"] <= DEDUP_SECONDS}
        st["dedup"][fingerprint(op, args)] = {"at": now, "result": result}
        self._save(session, st)


class FileRecords(MemoryRecords):
    """The same records, one ``<session>.json`` per session under ``directory``."""

    def __init__(self, directory: str | os.PathLike[str]) -> None:
        super().__init__()
        self.directory = Path(directory)

    def _path(self, session: str) -> Path:
        return self.directory / (session.replace("/", "_") + ".json")

    def _get(self, session: str) -> dict[str, Any]:
        try:
            return json.loads(self._path(session).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {"refusal": None, "count": 0, "dedup": {}}

    def _save(self, session: str, state: dict[str, Any]) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self.directory, suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(state, fh)
        os.replace(tmp, self._path(session))
