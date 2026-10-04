"""The databricks provider: one snapshot per environment (scope = env name), fetched in the background only
(spec v2 §15). Calls the CLI through dbcli.run_json, which enforces the read-only allowlist and host pinning."""
from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path

from filelock import FileLock

from orch.addons.api import Snapshot

from . import items as it
from .config import env_by_name, parse_envs
from .dbcli import DbxError, login_command, run_json
from .simulated import simulated_snapshot

RUNS_WINDOW = timedelta(hours=24)
RUNS_LIMIT = "50"
LIST_LIMIT = "100"
_HEALTH = {"auth_required": "auth_required", "offline": "offline", "host": "error", "error": "error"}


def token_cache_mtime() -> float | None:
    try:
        return (Path.home() / ".databricks" / "token-cache.json").stat().st_mtime
    except OSError:
        return None


class AuthHold:
    """After a login failure, no CLI call for that env until "I logged in" clears the hold or the CLI's token
    cache file changes (spec v2 §15, ruling R7). Lives in the addon's state folder."""

    def __init__(self, state_dir):
        self.path = Path(state_dir) / "auth-hold.json"

    def _read(self) -> dict:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def _update(self, change) -> object:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with FileLock(str(self.path.with_name(self.path.name + ".lock")), timeout=10):
            data = self._read()
            result = change(data)
            tmp = self.path.with_name(self.path.name + ".tmp")
            tmp.write_text(json.dumps(data), encoding="utf-8")
            tmp.replace(self.path)
        return result

    def hold(self, env: str, mtime: float | None) -> None:
        self._update(lambda data: data.__setitem__(env, {"mtime": mtime}))

    def clear(self, env: str) -> bool:
        return self._update(lambda data: data.pop(env, None) is not None)

    def waiting(self, env: str, mtime: float | None) -> bool:
        entry = self._read().get(env)
        return isinstance(entry, dict) and entry.get("mtime") == mtime


def _rows(data, key: str) -> list[dict]:
    rows = data if isinstance(data, list) else (data.get(key) if isinstance(data, dict) else None)
    return [r for r in (rows or []) if isinstance(r, dict)][:200]


def _folders(ctx) -> list[Path]:
    root = Path(ctx.root)
    out = [root]
    for spec in ((ctx.addon.ws.config.get("git") or {}).get("repos") or {}).values():
        if isinstance(spec, dict) and isinstance(spec.get("path"), str):
            path = (root / spec["path"]).resolve()
            if path not in out:
                out.append(path)
    return out[:10]


class DatabricksProvider:
    id = "databricks"
    kind = "status"
    interval_s = 300

    def scopes(self, ctx) -> list[str]:
        return [env.name for env in parse_envs(ctx.settings)]

    def fetch(self, ctx, scope, previous):
        now = ctx.now()
        env = env_by_name(ctx.settings, scope)
        if env is None:
            return Snapshot(self.id, scope, now, health="error", message=f"{scope} is no longer in the settings")
        if env.problem:
            return Snapshot(self.id, scope, now, health="error", message=env.problem)
        if env.simulated:
            return simulated_snapshot(self.id, env, now, demo=ctx.settings.get("demo") is True)
        if not env.live:  # run_json needs a profile and a pinned host; only live envs reach the CLI
            return Snapshot(self.id, scope, now, health="error", message=f"{scope}: check the settings")
        return self._live(ctx, env, now)

    def _failed(self, hold, env, now, error: DbxError, mtime) -> Snapshot:
        if error.kind == "auth_required":
            hold.hold(env.name, mtime)
            return Snapshot(self.id, env.name, now, health="auth_required", message=f"login needed: {login_command(env)}")
        return Snapshot(self.id, env.name, now, health=_HEALTH[error.kind], message=error.message)

    def _live(self, ctx, env, now) -> Snapshot:
        hold = AuthHold(ctx.addon.state_dir)
        mtime = token_cache_mtime()
        if hold.waiting(env.name, mtime):
            return Snapshot(self.id, env.name, now, health="auth_required",
                            message=f"login needed: {login_command(env)}, then press I logged in")
        try:
            me = (run_json(ctx, env, ("current-user", "me")) or {}).get("userName")
        except DbxError as e:
            return self._failed(hold, env, now, e, mtime)
        hold.clear(env.name)
        found = [it.env_item(env, me, it.local_connect_version(_folders(ctx)))]
        problems = []
        since = str(int((now - RUNS_WINDOW).timestamp() * 1000))
        sections = (
            ("runs", ("jobs", "list-runs"), ("--start-time-from", since, "--limit", RUNS_LIMIT),
             lambda d: [it.run_item(env.name, r, env.host) for r in _rows(d, "runs")]),
            ("pipelines", ("pipelines", "list-pipelines"), ("--limit", LIST_LIMIT),
             lambda d: [it.pipeline_item(env.name, p, env.host, now) for p in _rows(d, "statuses")]),
            ("warehouses", ("warehouses", "list"), (),
             lambda d: [it.warehouse_item(env.name, w) for w in _rows(d, "warehouses")]),
            ("clusters", ("clusters", "list"), ("--limit", LIST_LIMIT),
             lambda d: [it.cluster_item(env.name, c, me) for c in _rows(d, "clusters")]),
        )
        for section, command, args, convert in sections:
            try:
                found.extend(convert(run_json(ctx, env, command, *args)))
            except DbxError as e:
                if e.kind in ("auth_required", "offline", "host"):
                    return self._failed(hold, env, now, e, mtime)
                problems.append(f"{section}: {e.message}")
                found.append(it.problem_item(section, e.message))
        return Snapshot(self.id, env.name, now, health="error" if problems else "ok",
                        message="; ".join(problems)[:500], complete=not problems, me=me, items=tuple(found))
