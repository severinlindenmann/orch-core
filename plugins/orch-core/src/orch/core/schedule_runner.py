"""The schedules runner (docs/schedules.md): one `tick` starts the runs that are due and ends the ones that are done,
inside the dashboard server the human started. It refuses any process with an agent harness in its ancestry.

It never arms, files or decides anything. It works only for schedules whose signed charter still matches the
definition and the skill as they are now (orch.core.schedules.state), while the ledger is whole, the `schedules`
addon is switched on and the user-scope Claude settings carry orch's guard. Its limits: one run per schedule at a
time, MAX_CONCURRENT runs in all, the definition's runs per day (markers beside the ledger) and minutes per run.

- **schedule**: due when a slot of `when` passed since the last round. Slots missed while the dashboard was closed are
  not caught up one by one: the newest one runs once, and the run says how many it stands for.
- **listener**: due when an orch event after its cursor matches `on`, and its cooldown has passed. Events that match
  while a run goes on or the cooldown holds fold into the next run.
- **recurring**: no session. Due like a schedule; the run is a finding on Today with the template's ticket, which
  only the human files.

A run is one headless session (`claude -p`) through the factory runner's launch path: the program resolved to a
trusted absolute path, `env -i` with a fixed PATH and the allowlisted variables, user-scope settings only, no MCP
server but the ones the definition names from the user's own `schedule-mcp.json` in the orch config dir, and one tool
allowance, `orch schedule report`. The prompt is built in; only validated ids, the skill name and the event's kind,
ticket key and status reach it, never ticket text. The launcher is a parameter, so tests never start a real agent.
"""
from __future__ import annotations

import json
import secrets
from datetime import datetime, timedelta

from orch.core import factory_runner, factory_sessions as fs, ledger, schedules as sc
from orch.errors import OrchError

MAX_CONCURRENT = 2
REPORT_TOOL = "Bash(orch schedule report:*)"
MCP_FILE = "schedule-mcp.json"


def _now(now: datetime | None) -> datetime:
    return (now or datetime.now()).astimezone()


def _iso(s) -> datetime | None:
    try:
        return datetime.fromisoformat(s) if isinstance(s, str) else None
    except ValueError:
        return None


def prompt(d: sc.Definition, r: dict, token: str) -> str:
    """The built-in prompt. Never the workspace config or ticket text: an agent can edit those."""
    why = {"clock": "its time came", "manual": "the human asked for a run now", "event": "an orch event matched"}
    lines = [f"This is an unattended run of the orch schedule `{d.id}` (run {r['id']}), started because "
             f"{why.get(r['trigger'], 'it was due')}."]
    ev = r.get("event")
    if ev:
        lines.append(f"The event: {ev['kind']}" + (f" on ticket {ev['ticket']}" if ev.get("ticket") else "")
                     + (f" ({ev['filter']} {ev['value']})" if ev.get("filter") else "") + ".")
    if r.get("skipped"):
        lines.append(f"It stands for {r['skipped'] + 1} slots or events: the others came while no run could start.")
    lines += [
        f"Use the skill `{d.skill}` of this workspace (.claude/skills/{d.skill}/SKILL.md) and do what it says.",
        "Nobody answers questions during this run. Do not claim, move or edit tickets, do not commit or push, and do "
        "not start other agents. If the skill needs something you cannot do, say so in your report.",
        "When you are done, report exactly once. If something needs the human, pass your findings as YAML on stdin:",
        f"  orch schedule report {token} --file - <<'EOF'",
        *("  " + x for x in sc.REPORT_HELP.rstrip("\n").split("\n")),
        "  EOF",
        f"If nothing needs the human, run: orch schedule report {token} --quiet --summary \"<one line>\"",
        "Then stop.",
    ]
    return "\n".join(lines)


def _mcp_file(ws, d: sc.Definition, rid: str) -> tuple[str | None, str | None]:
    """(path of this run's MCP config, why not). The servers come only from the user's own schedule-mcp.json beside
    the ledger, never from the workspace (an agent can write .mcp.json); the definition only names which."""
    if not d.mcp:
        return None, None
    src = sc._read_json(ledger.base_dir() / MCP_FILE)
    servers = (src or {}).get("mcpServers")
    if not isinstance(servers, dict):
        return None, f"it names MCP servers but {ledger.base_dir() / MCP_FILE} has no mcpServers"
    missing = [m for m in d.mcp if not isinstance(servers.get(m), dict)]
    if missing:
        return None, f"MCP server {', '.join(missing)} is not in {MCP_FILE}"
    path = sc._root(ws) / "mcp" / f"{rid}.json"
    sc._write_json(path, {"mcpServers": {m: servers[m] for m in d.mcp}})
    return str(path), None


def argv_for(ws, d: sc.Definition, r: dict, token: str, session: str) -> tuple[list[str] | None, str | None]:
    """The command of one run, or (None, why not). Every part is a whole argv element: never a shell."""
    claude, env_bin = factory_runner.resolve_bin("claude"), factory_runner.resolve_bin("env")
    if claude is None or env_bin is None:
        return None, "claude or env was not found at a trusted path (owned by you or root, not writable by others)"
    orch_bin = factory_runner.resolve_bin("orch")  # so the run can report; optional beside claude's own folder
    mcp, why = _mcp_file(ws, d, r["id"])
    if why:
        return None, why
    bins = [claude] + ([orch_bin] if orch_bin else [])
    # The prompt comes right after -p: --allowedTools and --mcp-config take several values and would swallow it.
    argv = [*factory_runner.env_prefix(env_bin, bins), claude, "-p", prompt(d, r, token),
            "--allowedTools", REPORT_TOOL, *(["--mcp-config", mcp] if mcp else []),
            "--setting-sources", "user", "--strict-mcp-config", "--session-id", session,
            *(["--model", d.model] if d.model else [])]
    return argv, None


def _event_view(ev, d: sc.Definition) -> dict:
    from orch.core.constants import STATUSES
    from orch.dashboard.data.agent_start import KEY_RE
    out = {"seq": ev.seq, "kind": ev.kind}
    if isinstance(ev.ticket, str) and KEY_RE.fullmatch(ev.ticket):
        out["ticket"] = ev.ticket
    filt = sc.LISTEN.get(ev.kind)
    val = (ev.data or {}).get(filt) if filt else None
    if filt and isinstance(val, str) and (val in STATUSES or val in ("requirements", "plan")):
        out["filter"], out["value"] = filt, val
    return out


def _matches(ev, d: sc.Definition) -> bool:
    if ev.kind != d.on.get("event"):
        return False
    filt = sc.LISTEN.get(ev.kind)
    if filt and d.on.get(filt):
        return (ev.data or {}).get(filt) in d.on[filt]
    return True


def _skip(ws, d, now, trigger, why, **kw) -> str:
    r, _ = sc.start_record(ws, d, trigger=trigger, now=now, **kw)
    sc.finish(ws, r, "skipped", why, now)
    return f"{d.id}: skipped, {why}"


def _due(ws, d: sc.Definition, now: datetime, running: bool, last_start: datetime | None) -> tuple | None:
    """(trigger, extras, advance) when `d` should run now, else None. `advance` moves the cursor once the run is
    started or skipped for good."""
    c = sc.cursor(ws, d.id)
    if c.get("requested"):
        return "manual", {}, lambda: sc.set_cursor(ws, d.id, requested=False)
    if d.kind == "listener":
        from orch.core import events
        if not isinstance(c.get("seq"), int):
            sc.set_cursor(ws, d.id, seq=events.last_seq(ws))  # armed now: only what happens from here on counts
            return None
        evs = events.read_events(ws, after=c["seq"])
        hits = [e for e in evs if _matches(e, d)]
        if not hits:
            if evs:
                sc.set_cursor(ws, d.id, seq=evs[-1].seq)
            return None
        cool = timedelta(minutes=d.limits.get("cooldown_minutes", 0))
        if running or (last_start and now < last_start + cool):
            return None  # fold into the next run
        last = evs[-1].seq
        return "event", {"event": _event_view(hits[-1], d), "skipped": len(hits) - 1}, \
            lambda: sc.set_cursor(ws, d.id, seq=last)
    since = _iso(c.get("since"))
    if since is None:
        sc.set_cursor(ws, d.id, since=now.isoformat(timespec="seconds"))
        return None
    slot, n = sc.last_slot(d.when, now, since)
    if slot is None:
        return None
    return "clock", {"slot": slot.isoformat(timespec="minutes"), "skipped": n - 1}, \
        lambda: sc.set_cursor(ws, d.id, since=now.isoformat(timespec="seconds"))


def _recurring(ws, d: sc.Definition, now: datetime, trigger: str, extras: dict) -> str:
    """A recurring schedule's run: the template's ticket as a finding the human may file. No session."""
    r, _ = sc.start_record(ws, d, trigger=trigger, now=now, **extras)
    t = dict(d.ticket)
    t["title"] = " ".join(sc.render_title(t["title"], now).split())[:200]
    f = {"id": "f1", "title": f"{d.name} is due", "text": f"Recurring ticket from {d.path.name}.", "ticket": t,
         "state": sc.OPEN}
    f["sha"] = sc.finding_sha(f)
    r["findings"], r["reported"], r["summary"] = [f], True, f"{t['title']} is due"
    sc.finish(ws, r, "finding", "", now)
    return f"{d.id}: {t['title']} is due"


def _end(ws, r: dict, now: datetime, why: str | None = None) -> str:
    if why:
        sc.finish(ws, r, "failed", why, now)
        return f"{r['id']}: stopped, {why}"
    if not r.get("reported"):
        sc.finish(ws, r, "failed", "the session ended without a report", now)
        return f"{r['id']}: ended without a report"
    status = "finding" if r.get("findings") else "quiet"
    sc.finish(ws, r, status, "", now)
    return f"{r['id']}: {status}"


def _stop_reason(ws, r: dict, defs: dict, signed, cut: bool, enabled: bool, now: datetime) -> str | None:
    if not enabled:
        return "the schedules addon was switched off"
    if cut:
        return "the approval ledger on this machine was cut"
    d = defs.get(r.get("schedule"))
    if d is None:
        return "its definition is gone"
    st = sc.state(ws, d, signed)
    if not st.armed:
        return f"the schedule is {st.label}"
    started = _iso(r.get("started"))
    if started and now >= started + timedelta(minutes=d.limits["minutes_per_run"]):
        return f"it ran longer than {d.limits['minutes_per_run']} min"
    return None


def tick(ws, actor, launcher: factory_runner.Launcher, *, enabled: bool = True, now: datetime | None = None) -> list[str]:
    """One round; returns what it did, one line each."""
    fs.human_check(actor, "running schedules")
    now = _now(now)
    lines: list[str] = []
    open_runs = sc.open_runs(ws)
    if not enabled and not open_runs:
        return lines
    names = launcher.alive()
    if names is None:
        return lines  # tmux did not answer: conclude nothing, try again next round
    cut = not ledger.head_ok()
    signed = [] if cut else ledger.entries(ws)
    defs = {d.id: d for d in sc.definitions(ws)}
    keep = []
    for r in open_runs:
        name = r.get("session")
        alive = bool(name) and name in names
        if not alive:
            if r.get("status") == "starting":
                lines.append(_end(ws, r, now, "the session did not start"))
            else:
                lines.append(_end(ws, r, now))
            continue
        why = _stop_reason(ws, r, defs, signed, cut, enabled, now)
        if why is None:
            keep.append(r)
            continue
        try:
            launcher.stop(name)
        except (OrchError, OSError):
            pass
        lines.append(_end(ws, r, now, why))
    if not enabled or cut:
        return lines
    blocker = factory_runner.user_settings_blocker()
    day = sc.day_key(now)
    for d in defs.values():
        st = sc.state(ws, d, signed)
        if not st.armed:
            continue
        running = any(r.get("schedule") == d.id for r in keep)
        last = sc.runs(ws, d.id, limit=1)
        last_start = _iso(last[0].get("started")) if last else None
        due = _due(ws, d, now, running, last_start)
        if due is None:
            continue
        trigger, extras, advance = due
        if running:
            advance()
            lines.append(_skip(ws, d, now, trigger, "the previous run still goes on", **extras))
            continue
        if d.kind != "recurring" and len(keep) >= MAX_CONCURRENT:
            continue  # stays due: next round
        if d.kind != "recurring" and blocker:
            advance()
            lines.append(_skip(ws, d, now, trigger, blocker, **extras))
            continue
        if not sc.take_budget(ws, d.id, day, d.limits["runs_per_day"]):
            advance()
            lines.append(_skip(ws, d, now, trigger, f"the {d.limits['runs_per_day']} runs of today are used up",
                               **extras))
            continue
        advance()
        if d.kind == "recurring":
            lines.append(_recurring(ws, d, now, trigger, extras))
            continue
        r = _launch(ws, launcher, d, now, trigger, extras, lines)
        if r is not None:
            keep.append(r)
    sc.prune(ws)
    return lines


def _launch(ws, launcher, d: sc.Definition, now: datetime, trigger: str, extras: dict, lines: list) -> dict | None:
    r, token = sc.start_record(ws, d, trigger=trigger, now=now, **extras)
    session = fs.new_session_id()
    argv, why = argv_for(ws, d, r, token, session)
    if argv is None:
        sc.finish(ws, r, "failed", why, now)
        lines.append(f"{d.id}: not started, {why}")
        return None
    name = f"sc-{d.id[:24]}-{secrets.token_hex(3)}"
    r["session"], r["status"] = name, "running"
    sc.save_run(ws, r)
    try:
        launcher.start(name, str(ws.root.resolve()), argv)
    except (OrchError, OSError, ValueError) as e:
        try:
            launcher.stop(name)
        except (OrchError, OSError):
            pass
        sc.finish(ws, r, "failed", f"could not start: {e}", now)
        lines.append(f"{d.id}: could not start {name}: {e}")
        return None
    lines.append(f"{d.id}: started {name} ({trigger})")
    return r


def stop_all(ws, actor, launcher) -> list[str]:
    """The dashboard is stopping: every run ends now (it reported or it did not)."""
    fs.human_check(actor, "running schedules")
    names = launcher.alive() or set()
    lines = []
    now = _now(None)
    for r in sc.open_runs(ws):
        if r.get("session") in names:
            try:
                launcher.stop(r["session"])
            except (OrchError, OSError):
                pass
        lines.append(_end(ws, r, now, None if r.get("reported") else "the dashboard stopped"))
    return lines


def status(ws, now: datetime | None = None) -> dict:
    """What the addon's Today tile shows: armed schedules, runs today, open findings, next run."""
    now = _now(now)
    rows = sc.overview(ws, now)
    nxt = sorted(r["next"] for r in rows if r["next"])
    return {"armed": sum(1 for r in rows if r["state"] == "armed"),
            "needs_fix": sum(1 for r in rows if r["state"] in ("changed", "invalid")),
            "runs_today": sum(r["today"] for r in rows), "findings": len(sc.open_findings(ws)),
            "next": nxt[0] if nxt else None, "at": now.isoformat(timespec="seconds")}


def write_status(ws, path) -> None:
    """The status for the addon's tile, as a small file in the addon's own state folder (addons only read files)."""
    from orch.core.fsutil import atomic_write_text
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(path, json.dumps(status(ws)) + "\n")


