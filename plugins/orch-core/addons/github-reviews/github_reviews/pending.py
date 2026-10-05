"""Actions the human just ran (Rerun failed, Mark ready), one `<repo>#<number>` -> ISO time per entry in the addon's
state folder. Until a fetch newer than the action has landed, the table says "requested" instead of offering the same
button again; the cache itself stays the source of truth and is never edited."""
from __future__ import annotations

import json
import os
from datetime import datetime, timedelta
from pathlib import Path

from orch.clock import now

FILE = "pending.json"
KEEP = timedelta(hours=1)  # a fetch that never lands must not hide the button for good


def _load(state_dir) -> dict:
    try:
        data = json.loads((Path(state_dir) / FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def record(state_dir, target: str) -> None:
    folder = Path(state_dir)
    folder.mkdir(parents=True, exist_ok=True)
    stamp = now()
    data = {k: v for k, v in _load(folder).items() if isinstance(v, str) and _parse(v) and stamp - _parse(v) < KEEP}
    data[str(target)] = stamp.isoformat()
    tmp = folder / (FILE + ".tmp")
    tmp.write_text(json.dumps(data, sort_keys=True), encoding="utf-8")
    os.replace(tmp, folder / FILE)


def _parse(value) -> datetime | None:
    try:
        dt = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    return dt if dt.tzinfo is not None else None


def pending(state_dir, target: str, fetched_at) -> bool:
    """True while `target` was acted on after the newest fetch (`fetched_at`) and not longer than KEEP ago."""
    at = _parse(_load(state_dir).get(target))
    if at is None or now() - at >= KEEP:
        return False
    return fetched_at is None or fetched_at <= at
