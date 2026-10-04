"""Dismissed "may need an update" hints, per ticket|space|page (ruling R13). Written only from human POSTs."""
from __future__ import annotations

import json
from pathlib import Path

from filelock import FileLock

from orch.clock import stamp

MAX_ENTRIES = 5000


class Dismissed:
    def __init__(self, state_dir):
        self.path = Path(state_dir) / "dismissed.json"

    def _read(self) -> dict:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def keys(self) -> set[str]:
        return set(self._read())

    def add(self, key: str) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with FileLock(str(self.path.with_name(self.path.name + ".lock")), timeout=10):
            data = self._read()
            data[key] = stamp()
            if len(data) > MAX_ENTRIES:
                data = dict(sorted(data.items(), key=lambda kv: str(kv[1]))[-MAX_ENTRIES:])
            tmp = self.path.with_name(self.path.name + ".tmp")
            tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
            tmp.replace(self.path)
