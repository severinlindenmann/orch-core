"""Widgets of the databricks addon (canvas "Addon: Databricks", spec v2 §15, §16). Reads cached snapshots and
~/.databrickscfg only; never runs a command. Every value from the cache is treated as untrusted: roles are
checked, strings are cut, URLs must be safe."""
from __future__ import annotations

import re
from datetime import datetime

from orch.addons.widgets import KV, ROLES, Action, Badge, Callout, Card, Copy, Link, Table, Text, Tile, safe_url
from orch.clock import now as clock_now

from .config import owned, parse_envs, prefixes, scope_mode
from .dbcfg import profiles
from .dbcli import login_command

PROVIDER = "databricks"
ROLE_ORDER = {"err": 0, "warn": 1, "info": 2, "neu": 3, "ok": 4}
AUTH_BADGE = {"ok": ("ok", "signed in"), "auth_required": ("warn", "login needed"),
              "never_fetched": ("neu", "not checked yet"), "offline": ("warn", "offline"), "error": ("err", "error"),
              "stale": ("warn", "stale"), "rate_limited": ("warn", "rate limited")}
CARD_ROLE = {"ok": "ok", "auth_required": "warn", "offline": "warn", "stale": "warn", "rate_limited": "warn", "error": "err"}
UNKNOWN_WHY = {"auth_required": "login needed", "never_fetched": "not fetched yet", "offline": "offline",
               "error": "error", "stale": "stale", "rate_limited": "rate limited"}
ACCESS = {"can_use": ("ok", "You can use"), "no_access": ("neu", "No access"), "unknown": ("neu", "Unknown")}
RUN_COLUMNS = ("Environment", "Run", "State", "Started", "Ticket", "Open")
_MM = re.compile(r"(\d+\.\d+)")


def _role(value) -> str:
    return value if value in ROLES else "neu"


def _str(value, cap: int = 200) -> str:
    return " ".join(str(value if value is not None else "").split())[:cap]


def _int(value) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _minutes(value) -> str:
    n = _int(value)
    return f"{n} min" if n and n > 0 else "off"


def _ts(value) -> float:
    try:
        dt = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return 0.0
    return dt.timestamp() if dt.tzinfo else 0.0


def ago(value) -> str:
    stamp = _ts(value)
    if not stamp:
        return "unknown"
    secs = max(0, int(clock_now().timestamp() - stamp))
    if secs < 90:
        return "just now"
    if secs < 5400:
        return f"{secs // 60} min ago"
    if secs < 172800:
        return f"{secs // 3600} h ago"
    return f"{secs // 86400} d ago"


class EnvView:
    def __init__(self, spec, snapshot):
        self.spec = spec
        self.snapshot = snapshot

    @property
    def name(self) -> str:
        return self.spec.name

    @property
    def health(self) -> str:
        return self.snapshot.health if self.snapshot else "never_fetched"

    @property
    def simulated(self) -> bool:
        return self.spec.simulated is not None

    def of(self, kind: str) -> list[dict]:
        return [i for i in (self.snapshot.items if self.snapshot else ()) if isinstance(i, dict) and i.get("type") == kind]

    @property
    def env_item(self) -> dict:
        found = self.of("env")
        return found[0] if found else {}

    @property
    def me(self) -> str | None:
        me = (self.snapshot.me if self.snapshot else None) or self.env_item.get("me")
        return me if isinstance(me, str) else None

    @property
    def fresh(self) -> bool:
        """The items come from the latest fetch. A fetch that fails as a whole (host changed, profile missing,
        offline, login) returns no items and no me, and the scheduler keeps the previous items under it; a fetch
        where only one section failed is still fresh and carries me."""
        if not self.snapshot:
            return False
        return self.health == "ok" or (self.health == "error" and self.snapshot.me is not None)

    @property
    def signed_in(self) -> bool:
        return self.fresh and bool(self.env_item)

    @property
    def has_items(self) -> bool:
        return bool(self.snapshot and self.snapshot.items)

    def label(self):
        return Badge("neu", f"{self.name} · simulated") if self.simulated else self.name


def env_views(view) -> list[EnvView]:
    snaps = {s.scope: s for s in view.snapshots(PROVIDER)}
    return [EnvView(spec, snaps.get(spec.name)) for spec in parse_envs(view.settings)]


def _visible(env: EnvView, item: dict, mode: str, names) -> bool:
    """Simulated envs are exempt from the ownership filter: fixture data belongs to no one."""
    return env.simulated or owned(item, mode, env.me, names)


def ticket_index(addon_ctx):
    entries = list(addon_ctx.tickets())
    by_key = {}
    for entry in entries:
        for x in (entry.meta or {}).get("external") or []:
            if isinstance(x, dict) and x.get("key"):
                by_key[str(x["key"]).upper()] = entry.id
    prefix = str((addon_ctx.ws.config.get("id") or {}).get("prefix") or "")
    pattern = re.compile(rf"\b{re.escape(prefix)}-\d+\b") if prefix else None
    return by_key, pattern, {entry.id for entry in entries}


def linked_ticket(run: dict, index) -> str | None:
    by_key, pattern, ids = index
    found = by_key.get(f"DBX-{run.get('run_id')}".upper())
    if found:
        return found
    m = pattern.search(str(run.get("name") or "")) if pattern else None
    return m.group(0) if m and m.group(0) in ids else None


def run_target(env: EnvView, run: dict) -> str | None:
    run_id, job_id = str(run.get("run_id") or ""), str(run.get("job_id") or "")
    if not (run_id.isdigit() and job_id.isdigit()):
        return None
    return f"{env.name}|{job_id}|{run_id}"  # a lookup key only; act() reads the run from the cache


def failing_runs(envs, mode, names, *, states=("failed", "running")) -> list[tuple[EnvView, dict]]:
    rows = [(e, r) for e in envs for r in e.of("run") if r.get("state") in states and _visible(e, r, mode, names)]
    rows.sort(key=lambda er: (er[1].get("state") != "failed", -_ts(er[1].get("started_at"))))
    return rows


def run_cells(env: EnvView, run: dict, index, *, with_open: bool = True) -> tuple:
    tid = linked_ticket(run, index)
    target = run_target(env, run)
    if tid:
        ticket = Link(tid, f"/t/{tid}")
    elif target and run.get("state") == "failed" and env.health == "ok":  # act() refuses anything else
        ticket = Action("create_ticket", "Create ticket", target)
    else:
        ticket = None
    cells = [env.label(), _str(run.get("name")) or "unnamed run",
             Badge(_role(run.get("role")), _str(run.get("text")) or "unknown"), ago(run.get("started_at")), ticket]
    if with_open:
        url = run.get("url")
        cells.append(Link("Open", url) if safe_url(url) else None)
    return tuple(cells)


def _unknown_note(envs) -> Text | None:
    gaps = [f"{e.name} ({'check the settings' if e.spec.problem else UNKNOWN_WHY.get(e.health, e.health)})"
            for e in envs if not e.signed_in]
    return Text(f"Unknown for {', '.join(gaps)}.") if gaps else None


def _empty(envs, text: str) -> str:
    return text if any(e.signed_in or e.has_items for e in envs) else "Unknown: no environment has data yet."


def runs_card(envs, mode, names, index) -> Card:
    rows = tuple(run_cells(e, r, index) for e, r in failing_runs(envs, mode, names)[:100])
    body = [Table(RUN_COLUMNS, rows, empty=_empty(envs, "No failed or running runs in the last 24 hours."))]
    note = _unknown_note(envs)
    if note:
        body.append(note)
    return Card("Failed and running job runs", tuple(body))


def pipelines_card(envs, mode, names) -> Card:
    found = [(e, p) for e in envs for p in e.of("pipeline") if _visible(e, p, mode, names)]
    found.sort(key=lambda ep: (ROLE_ORDER[_role(ep[1].get("role"))], _str(ep[1].get("name")).lower()))
    rows = tuple((e.label(), _str(p.get("name")) or "unnamed pipeline",
                  Badge(_role(p.get("role")), _str(p.get("text")) or "unknown"), ago(p.get("last_update_at")),
                  Link("Open", p["url"]) if safe_url(p.get("url")) else None) for e, p in found[:100])
    return Card("Pipelines", (Table(("Environment", "Pipeline", "Health", "Last update", "Open"), rows,
                                    empty=_empty(envs, "No pipelines in scope.")),))


def env_card(e: EnvView, mode, names) -> Card:
    item = e.env_item
    host = item.get("host") or e.spec.host
    runs = [r for r in e.of("run") if _visible(e, r, mode, names)]
    values = (sum(1 for r in runs if r.get("state") == "failed"), sum(1 for r in runs if r.get("state") == "running"),
              sum(1 for p in e.of("pipeline") if p.get("role") in ("err", "warn") and _visible(e, p, mode, names)))
    labels = ("Failed runs (24 h)", "Running", "Pipelines needing attention")
    role, text = AUTH_BADGE.get(e.health, ("neu", e.health))
    if e.simulated and e.health == "ok":
        text = "signed in (simulated)"
    rows = [("Source", Badge("neu", "simulated"))] if e.simulated else []
    rows += [("Profile", _str(item.get("profile") or e.spec.profile) or "—"),
             ("Host", Link(_str(host), host) if safe_url(host) else (_str(host) or "—")),
             ("Signed in as", (Text(f"{_str(e.me)} (last known)") if e.me and not e.signed_in else _str(e.me))
              or "unknown"),
             ("Auth", Badge(role, text))]
    if e.signed_in:
        rows += list(zip(labels, values))
    elif e.has_items:
        rows += [(label, Text(f"{value} (last known)")) for label, value in zip(labels, values)]
    else:
        rows += [(label, "unknown") for label in labels]
    body = [KV(tuple(rows))]
    if e.spec.problem:
        body.append(Callout("err", "Check the settings", _str(e.spec.problem, 500)))
    elif e.health in ("error", "offline") and e.snapshot and e.snapshot.message:
        body.append(Callout(CARD_ROLE[e.health], "Could not read this environment", _str(e.snapshot.message, 500)))
    if e.health == "auth_required" and e.spec.live:
        body.append(Copy("Copy re-login" if e.me else "Copy login", login_command(e.spec)))
        body.append(Action("logged_in", "I logged in", e.name))
    return Card(e.name, tuple(body), role=CARD_ROLE.get(e.health))


NO_REAL_DRIFT = "Deploy drift is not checked for real workspaces yet"


def drift_card(envs) -> Card:
    """Drift comes only from simulated fixtures: databricks bundle summary runs the repo's scripts and Python, so
    it is never called. A cached drift item of a real env (from an older version) is ignored."""
    rows = []
    for e in envs:
        if not e.simulated:
            rows.append((e.label(), NO_REAL_DRIFT, "unknown", "unknown", "unknown"))
            continue
        found = e.of("drift")
        if not found:
            rows.append((e.label(), Badge("neu", "unknown"), "unknown", "unknown", "unknown"))
            continue
        d = found[0]
        deployed, total = _int(d.get("deployed")), _int(d.get("total"))
        rows.append((e.label(), Badge(_role(d.get("role")), _str(d.get("text"), 300) or "unknown"),
                     f"{deployed} of {total}" if deployed is not None and total is not None else "unknown",
                     _str(d.get("commit")) or "unknown", ago(d.get("deployed_at")) if d.get("deployed_at") else "unknown"))
    return Card("Deploy drift", (Table(("Environment", "Drift", "Deployed resources", "Version", "Deployed"),
                                       tuple(rows), empty="No environments."),))


def _mm(version) -> str | None:
    m = _MM.match(str(version or ""))
    return m.group(1) if m else None


def _access(item: dict) -> Badge:
    access = item.get("access") if item.get("access") in ACCESS else "unknown"
    role, text = ACCESS[access]
    reason = _str(item.get("reason"), 120)
    return Badge(role, f"{text}: {reason}" if reason and access != "can_use" else text)


def rank_clusters(clusters, connect) -> list[dict]:
    want = _mm(connect)
    return sorted(clusters, key=lambda c: (c.get("access") != "can_use", not (want and c.get("dbr") == want),
                                           c.get("text") != "running", not _int(c.get("auto_stop")),
                                           _str(c.get("name")).lower()))


def compute_card(envs) -> Card:
    body = []
    for e in sorted(envs, key=lambda e: not e.signed_in):  # never lead with an env that is not signed in
        if not e.signed_in:
            why = "check the settings" if e.spec.problem else UNKNOWN_WHY.get(e.health, e.health)
            body.append(Text(f"{e.name}: sign in to see compute ({why})."))
            continue
        connect = e.env_item.get("connect_version") if isinstance(e.env_item.get("connect_version"), str) else None
        clusters = rank_clusters(e.of("cluster"), connect)
        best = next((c for c in clusters if c.get("access") == "can_use"), None)
        if best:
            match = bool(_mm(connect)) and best.get("dbr") == _mm(connect)
            rec = (f"Recommended: {_str(best.get('name'))} (DBR {_str(best.get('dbr')) or 'unknown'}"
                   + (f", matches your databricks-connect {connect})" if match else ")"))
        else:
            rec = "No cluster you can use for sure. SQL warehouses below work for SQL."
        if not connect:
            rec += " Local databricks-connect version: not found."
        rows = [(_str(c.get("name")) or "cluster", "cluster", Badge(_role(c.get("role")), _str(c.get("text")) or "unknown"),
                 _access(c), _str(c.get("dbr")) or "unknown", _minutes(c.get("auto_stop")),
                 Copy("Copy cluster ID", _str(c.get("cluster_id"))) if c.get("cluster_id") else None) for c in clusters]
        rows += [(_str(w.get("name")) or "warehouse", f"SQL warehouse {_str(w.get('size'))}".strip(),
                  Badge(_role(w.get("role")), _str(w.get("text")) or "unknown"), _access(w), "—", _minutes(w.get("auto_stop")),
                  Copy("Copy HTTP path", _str(w.get("http_path"))) if w.get("http_path") else None) for w in e.of("warehouse")]
        title = f"{e.name} · simulated" if e.simulated else e.name
        body.append(Card(title, (Text(rec), Table(("Name", "Kind", "State", "Access", "DBR", "Auto-stop", "Copy"),
                                                  tuple(rows[:100]), empty="No clusters or warehouses listed."))))
    return Card("Compute for local development", tuple(body) or (Text("No environments."),))


def scope_text(mode: str, names) -> str:
    if mode == "all":
        return "Showing all runs and pipelines in each workspace. Change it in Workspace & addons."
    if mode == "prefix":
        return (f"Showing runs and pipelines whose names start with {', '.join(names) or 'a saved prefix'}. "
                "Change it in Workspace & addons.")
    return "Showing runs and pipelines you created or run. Change it in Workspace & addons."


def no_envs_callout() -> Callout:
    found = list(profiles().items())[:20]
    listed = ", ".join(f"{p} ({h or 'no host'})" for p, h in found) or "none found; run databricks auth login first"
    return Callout("info", "No environments mapped yet",
                   "Map each environment to a profile in Workspace & addons, one per line: "
                   f"dev = <profile> @ <workspace host>. Profiles in ~/.databrickscfg: {listed}.")


def page(view, addon_ctx) -> list:
    envs = env_views(view)
    if not envs:
        return [no_envs_callout()]
    mode, names = scope_mode(view.settings), prefixes(view.settings)
    out = []
    simulated = [e.name for e in envs if e.simulated]
    if simulated:
        verb = "is" if len(simulated) == 1 else "are"
        out.append(Callout("neu", f"{', '.join(simulated)} {verb} simulated (demo)",
                           "Recorded data from the addon's fixtures, not a real workspace."))
    if mode == "prefix" and not names:
        where = " of real workspaces" if simulated else ""
        out.append(Callout("warn", "No name prefix saved",
                           f"Scope is prefix, but no prefix is saved, so no runs or pipelines{where} are shown."))
    out.append(Text(scope_text(mode, names)))
    index = ticket_index(addon_ctx)
    out += [runs_card(envs, mode, names, index), pipelines_card(envs, mode, names),
            Card("Environments", tuple(env_card(e, mode, names) for e in envs)), drift_card(envs), compute_card(envs)]
    return out


def today_summary(view) -> list:
    envs = env_views(view)
    n = len(failing_runs(envs, scope_mode(view.settings), prefixes(view.settings), states=("failed",)))
    return [Tile("Databricks failures", n, "err", href=f"/addons/{view.addon}/")] if n else []


def today_from_addons(view, addon_ctx) -> list:
    envs = env_views(view)
    rows = failing_runs(envs, scope_mode(view.settings), prefixes(view.settings), states=("failed",))
    if not rows:
        return []
    index = ticket_index(addon_ctx)
    n = len(rows)
    table = Table(RUN_COLUMNS[:5], tuple(run_cells(e, r, index, with_open=False) for e, r in rows[:5]))
    return [Card(f"Databricks: {n} failed run{'s' if n != 1 else ''}", (table,), role="err", href=f"/addons/{view.addon}/")]
