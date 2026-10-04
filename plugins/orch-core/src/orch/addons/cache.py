"""Snapshot cache (v2 §11.10): `state_dir/addons/<addon>/<provider>.<scope>.json`, written atomically. The change
marker that wakes live reload is rewritten only when a snapshot's content hash changes."""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

from orch.addons.api import Snapshot
from orch.clock import stamp_s
from orch.core.fsutil import atomic_write_text


def cache_dir(ws, addon: str) -> Path:
    return ws.state_dir / "addons" / addon


def scope_slug(scope: str) -> str:
    head = re.sub(r"[^A-Za-z0-9._-]", "_", scope)[:60]
    return f"{head}-{hashlib.sha1(scope.encode('utf-8')).hexdigest()[:8]}"


def cache_path(ws, addon: str, provider: str, scope: str) -> Path:
    return cache_dir(ws, addon) / f"{provider}.{scope_slug(scope)}.json"


def changed_marker(ws) -> Path:
    return ws.state_dir / "addons" / "changed"


def _read(path: Path) -> Snapshot | None:
    try:
        return Snapshot.from_dict(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, ValueError, UnicodeDecodeError):
        return None


def read_snapshot(ws, addon: str, provider: str, scope: str) -> Snapshot | None:
    return _read(cache_path(ws, addon, provider, scope))


def read_snapshots(ws, addon: str, provider: str | None = None) -> list[Snapshot]:
    folder = cache_dir(ws, addon)
    if not folder.is_dir():
        return []
    out = []
    for path in sorted(folder.glob("*.json")):
        s = _read(path)
        if s is not None and (provider is None or s.provider == provider):
            out.append(s)
    return out


def write_snapshot(ws, addon: str, snapshot: Snapshot) -> bool:
    old = read_snapshot(ws, addon, snapshot.provider, snapshot.scope)
    atomic_write_text(cache_path(ws, addon, snapshot.provider, snapshot.scope),
                      json.dumps(snapshot.to_dict(), ensure_ascii=False) + "\n")
    changed = old is None or old.content_hash() != snapshot.content_hash()
    if changed:
        atomic_write_text(changed_marker(ws), f"{stamp_s()} {addon} {snapshot.provider} {snapshot.scope}\n")
    return changed
