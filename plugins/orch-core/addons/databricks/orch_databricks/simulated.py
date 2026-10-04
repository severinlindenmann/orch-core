"""Simulated environments for demos (spec A1 §7.3): a recorded snapshot from fixtures/simulated/<name>.json.
Only selectable in settings, only served in a workspace marked as a demo (ruling R4). Every item carries
simulated: true; *_minutes_ago keys become datetimes relative to now."""
from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path

from orch.addons.api import Snapshot

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "simulated"
_TIME_KEYS = {"started_minutes_ago": "started_at", "ended_minutes_ago": "ended_at",
              "last_update_minutes_ago": "last_update_at", "deployed_minutes_ago": "deployed_at"}


def available() -> list[str]:
    return sorted(p.stem for p in FIXTURES.glob("*.json"))


def simulated_snapshot(provider_id: str, env, now, *, demo: bool) -> Snapshot:
    if not demo:
        return Snapshot(provider_id, env.name, now, health="error",
                        message=(f"{env.name} is simulated, but this workspace is not marked as a demo; turn on "
                                 f"Demo workspace in Workspace & addons or map {env.name} to a profile"))
    try:
        data = json.loads((FIXTURES / f"{env.simulated}.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return Snapshot(provider_id, env.name, now, health="error",
                        message=f"no simulated fixture {env.simulated!r} (available: {', '.join(available()) or 'none'})")
    if not isinstance(data, dict):
        data = {}
    items = []
    for raw in data.get("items") or []:
        if not isinstance(raw, dict):
            continue
        item = {k: v for k, v in raw.items() if k not in _TIME_KEYS}
        for src, dst in _TIME_KEYS.items():
            if isinstance(raw.get(src), (int, float)) and not isinstance(raw.get(src), bool):
                item[dst] = (now - timedelta(minutes=raw[src])).isoformat()
        item["env"] = env.name
        item["simulated"] = True
        if item.get("type") == "env":
            item["label"] = env.name
        items.append(item)
    me = data.get("me") if isinstance(data.get("me"), str) else None
    return Snapshot(provider_id, env.name, now, me=me, message=f"{env.name} is simulated (demo)", items=tuple(items))
