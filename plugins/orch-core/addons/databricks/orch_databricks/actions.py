"""Human POSTs of the databricks addon. They are declared in the manifest, and orch calls act() only from a
dashboard POST with the human actor (spec A1 §5.7)."""
from __future__ import annotations

import json
import re

from orch.addons.api import Snapshot
from orch.errors import ValidationError

from .config import HOST, env_by_name, normalize_host
from .provider import AuthHold

# env|job_id|run_id. Pages rendered before the key lost its name part may still post `|<name>`: it is ignored.
_RUN = re.compile(r"([a-z][a-z0-9_-]{0,19})\|(\d{1,20})\|(\d{1,20})(?:\|.*)?", re.S)
PROVIDER = "databricks"


def _existing(addon_ctx, key: str) -> str | None:
    for entry in addon_ctx.tickets():
        for x in (entry.meta or {}).get("external") or []:
            if isinstance(x, dict) and str(x.get("key", "")).upper() == key:
                return entry.id
    return None


class NotCurrent(Exception):
    """The run is cached, but under a snapshot whose fetch did not succeed: the data may be stale."""


def _snapshot(addon_ctx, env_name: str) -> Snapshot | None:
    for path in sorted(addon_ctx.state_dir.glob(f"{PROVIDER}.*.json")):
        try:
            snap = Snapshot.from_dict(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, ValueError, UnicodeDecodeError):
            continue
        if snap.provider == PROVIDER and snap.scope == env_name:
            return snap
    return None


def cached_run(addon_ctx, env_name: str, job_id: str, run_id: str) -> tuple[dict, str | None] | None:
    """(run item, workspace host) from the addon's own cached snapshot for env_name, or None. The target of a
    POST is only a lookup key: everything a ticket says comes from here, the host from the snapshot's env item.
    Raises NotCurrent when the snapshot's health is not ok (kept items of a failed fetch)."""
    snap = _snapshot(addon_ctx, env_name)
    if snap is None:
        return None
    items = [i for i in snap.items if isinstance(i, dict)]
    run = next((i for i in items if i.get("type") == "run" and str(i.get("run_id")) == run_id
                and str(i.get("job_id")) == job_id), None)
    if run is None:
        return None
    if snap.health != "ok":
        raise NotCurrent(env_name)
    env_item = next((i for i in items if i.get("type") == "env"), {})
    host = normalize_host(env_item.get("host"))
    return run, (host if HOST.fullmatch(host) else None)


def create_ticket(target: str, ctx) -> str:
    m = _RUN.fullmatch(target or "")
    if not m:
        raise ValidationError("that run reference is not valid; reload the page")
    env_name, job_id, run_id = m.groups()
    try:
        found = cached_run(ctx.addon, env_name, job_id, run_id)
    except NotCurrent:
        raise ValidationError(f"the data for {env_name} is not current (the last fetch failed); "
                              "fix the environment, then reload the page") from None
    if found is None:
        raise ValidationError(f"run {run_id} is not in the latest data for {env_name}; reload the page")
    run, host = found
    if run.get("state") != "failed":
        raise ValidationError(f"run {run_id} did not fail; only failed runs get a ticket")
    env = env_by_name(ctx.settings, env_name)
    if env is None:
        raise ValidationError(f"{env_name!r} is not a Databricks environment in this workspace")
    key = f"DBX-{run_id}"
    found = _existing(ctx.addon, key)
    if found:
        return f"{found} already tracks run {run_id}."
    where = f"{env_name} (simulated)" if env.simulated else env_name
    url = f"{host}/#job/{job_id}/run/{run_id}" if env.live and host else None
    name = " ".join(str(run.get("name") or "").split())[:120]
    title = f"Fix failed Databricks run {name or run_id} in {env_name}"
    ask = f"The Databricks job run {run_id} (job {job_id}) failed in {where}." + (f" Run: {url}" if url else "")
    tid = ctx.addon.ops().import_external(key, title, ask=ask)
    return f"Created {tid} for run {run_id}."


def logged_in(target: str, ctx) -> str:
    env = env_by_name(ctx.settings, target or "")
    if env is None or not env.live:
        raise ValidationError(f"{target!r} is not a Databricks environment in this workspace")
    AuthHold(ctx.addon.state_dir).clear(env.name)
    return f"Checking the login for {env.name} at the next refresh."


ACTIONS = {"create_ticket": create_ticket, "logged_in": logged_in}


def act(action_id: str, target: str, ctx) -> str:
    fn = ACTIONS.get(action_id)
    if fn is None:
        raise ValidationError(f"unknown action {action_id!r}")
    return fn(target, ctx)
