"""`orch schedule …` (docs/schedules.md): agent work on a clock, on an orch event, or as a recurring ticket.
Anyone may `list`, `show`, `runs`, `check`, `findings` and `pause`; a run `report`s with its token. `arm`, `resume`,
`run-now`, `file` and `dismiss` are the human's (a terminal, outside an agent harness; the guard denies them to
agents)."""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Annotated, Optional

import typer

schedule_app = typer.Typer(no_args_is_help=True, help="Schedules: skills on a clock or an event, recurring tickets.")

JsonOpt = Annotated[bool, typer.Option("--json", help="Machine-readable output.")]


def _ctx():
    from orch import cli
    return cli, cli._ws()


def _when(iso) -> str:
    return iso.replace("T", " ")[:16] if isinstance(iso, str) else "-"


@schedule_app.command("list")
def list_(json_out: JsonOpt = False) -> None:
    """Every schedule with its kind, state, trigger and next run."""
    from orch.core import schedules
    cli, ws = _ctx()
    rows = schedules.overview(ws)
    for r in rows:
        r.pop("recent", None)
    lines = [f"{r['id']:<22} {r['kind']:<10} {r['label']:<12} {r['trigger'] or r['why']}"
             + (f" · next {_when(r['next'])}" if r["next"] else "")
             + (f" · last {r['last']['status']}" if r.get("last") else "") for r in rows]
    folder = schedules.folder(ws).relative_to(ws.root)
    cli._out(rows, json_out, "\n".join(lines) or f"no schedules: add one as {folder}/<id>.yaml (docs/schedules.md)")


@schedule_app.command("show")
def show(sid: str, json_out: JsonOpt = False) -> None:
    """One schedule: its definition as orch reads it, its state and what arming it would sign."""
    from orch.core import schedules
    cli, ws = _ctx()
    row = next((r for r in schedules.overview(ws) if r["id"] == sid), None)
    if row is None:
        schedules.get(ws, sid)  # raises NotFoundError with the hint
    row.pop("recent", None)
    lines = [f"{row['name']} ({row['id']}, {row['kind']}) · {row['label']}",
             f"  file     {row['path']}  {row['def_sha'][:19]}…"]
    if row["skill"]:
        lines.append(f"  skill    .claude/skills/{row['skill']}  " + (f"{row['skill_sha'][:19]}…" if row["skill_sha"] else "missing"))
    lines.append(f"  trigger  {row['trigger'] or '-'}" + (f" · next {_when(row['next'])}" if row["next"] else ""))
    lims = row["limits"]
    lines.append(f"  limits   {lims.get('runs_per_day')} runs a day, {lims.get('minutes_per_run')} min a run"
                 + (f", cooldown {lims['cooldown_minutes']} min" if "cooldown_minutes" in lims else "")
                 + f" · {row['today']} today")
    if row["mcp"]:
        lines.append(f"  mcp      {', '.join(row['mcp'])}")
    if row["ticket"]:
        lines.append(f"  ticket   {row['ticket']['title']} ({row['ticket']['type']}, {row['ticket']['priority']})")
    if row["why"]:
        lines.append(f"  note     {row['why']}")
    cli._out(row, json_out, "\n".join(lines))


@schedule_app.command("runs")
def runs(sid: Annotated[Optional[str], typer.Argument(help="Only this schedule's runs.")] = None,
         last: Annotated[int, typer.Option("--last", help="How many, newest first.")] = 20,
         json_out: JsonOpt = False) -> None:
    """Run history: result, duration and the run's summary."""
    from orch.core import schedules
    cli, ws = _ctx()
    rs = schedules.runs(ws, sid, limit=max(1, min(last, 200)))
    for r in rs:
        r.pop("token", None)
    lines = [f"{r['id']}  {r['schedule']:<20} {r['status']:<9} {_when(r.get('started'))}  "
             + (r.get("summary") or r.get("reason") or "") for r in rs]
    cli._out(rs, json_out, "\n".join(lines) or "no runs yet")


@schedule_app.command("check")
def check(sid: Annotated[Optional[str], typer.Argument(help="One schedule; default all.")] = None,
          json_out: JsonOpt = False) -> None:
    """Agents and humans: is the definition valid, and is its skill there? Use it after writing a schedule; a valid
    one still runs only after the human arms it."""
    from orch.core import schedules
    from orch.errors import ValidationError
    cli, ws = _ctx()
    ds = [schedules.get(ws, sid)] if sid else schedules.definitions(ws)
    out = []
    for d in ds:
        _, why = schedules.skill_hash(ws, d.skill)
        problems = [*d.problems, *([why] if why else [])]
        out.append({"id": d.id, "ok": not problems, "problems": problems})
    text = "\n".join(f"{o['id']}: " + ("ok" if o["ok"] else "; ".join(o["problems"])) for o in out) or "no schedules"
    cli._out(out, json_out, text)
    if any(not o["ok"] for o in out):
        raise ValidationError("some schedules need a fix")


@schedule_app.command("arm")
def arm(sid: str, json_out: JsonOpt = False) -> None:
    """Human only: sign the schedule as it stands (its definition and every file of its skill). Any later change
    pauses it until you arm it again."""
    from orch.actor import confirm_typed, require_human_terminal
    from orch.core import schedules
    cli, ws = _ctx()
    require_human_terminal("arming a schedule")
    d = schedules.get(ws, sid)
    st = schedules.state(ws, d)
    typer.echo(f"Arming {d.id} ({d.kind}, {schedules.describe(d) if d.ok else 'invalid'}):")
    typer.echo(f"  definition {d.path.relative_to(ws.root)}  {st.def_sha}")
    if d.skill:
        typer.echo(f"  skill      .claude/skills/{d.skill}  {st.skill_sha or 'missing'}")
    typer.echo(f"  limits     {d.limits.get('runs_per_day')} runs a day, {d.limits.get('minutes_per_run')} min a run")
    actor = confirm_typed(d.id)
    value = schedules.arm(ws, actor, sid, expected_def=st.def_sha, expected_skill=st.skill_sha or "")
    cli._out(value, json_out, f"{sid} armed; it runs while Mission Control runs with the schedules addon on")


@schedule_app.command("resume")
def resume(sid: str, json_out: JsonOpt = False) -> None:
    """Human only: the same as arm, for a paused or changed schedule."""
    arm(sid, json_out)


@schedule_app.command("pause")
def pause(sid: str, json_out: JsonOpt = False) -> None:
    """Anyone: stop a schedule from running (signed, like any switch off). Only the human resumes it."""
    from orch.actor import cli_actor
    from orch.core import schedules
    cli, ws = _ctx()
    schedules.pause(ws, cli_actor(), sid)
    cli._out({"id": sid, "state": "paused"}, json_out, f"{sid} paused; `orch schedule resume {sid}` (human) arms it again")


@schedule_app.command("run-now")
def run_now(sid: str, json_out: JsonOpt = False) -> None:
    """Human only: one run outside the clock, counted against today's budget. Mission Control's runner starts it
    within a round."""
    from orch.actor import human_actor, require_human_terminal
    from orch.core import schedules
    cli, ws = _ctx()
    require_human_terminal("starting a schedule run by hand")
    schedules.request_run(ws, human_actor(sid), sid)
    cli._out({"id": sid, "requested": True}, json_out, f"{sid}: a run starts within a round while Mission Control runs")


@schedule_app.command("report")
def report(token: str,
           file: Annotated[Optional[str], typer.Option("--file", help="The findings as YAML; - reads stdin.")] = None,
           quiet: Annotated[bool, typer.Option("--quiet", help="Nothing needs the human.")] = False,
           summary: Annotated[str, typer.Option("--summary", help="One line, with --quiet.")] = "",
           json_out: JsonOpt = False) -> None:
    """A run reports once, with the token from its prompt: findings as YAML (--file, or --file - for stdin), or
    --quiet when nothing needs the human.

    \b
    summary: One line about what the run did.
    findings:              # at most 10
      - title: What needs the human
        text: Why, with the facts that show it.
        ticket:            # optional, filed only by the human
          title: Fix the CSV export of umlauts
          ask: What should be done and why.
          type: bug        # feature, bug, chore, spike, investigation
          priority: high   # low, normal, high, urgent
    """
    from orch.core import schedules
    from orch.core.model import yaml_load
    from orch.errors import UsageError, ValidationError
    cli, ws = _ctx()
    if quiet == (file is not None):
        raise UsageError("pass exactly one of --file or --quiet")
    if quiet:
        data = {"summary": summary}
    else:
        text = sys.stdin.read(256 * 1024) if file == "-" else Path(file).read_text(encoding="utf-8")
        try:
            data = yaml_load(text)
        except Exception as e:
            raise ValidationError("the report is not valid YAML", hint=schedules.REPORT_HELP) from e
    r = schedules.report(ws, token, data, quiet=quiet)
    n = len(r["findings"])
    cli._out({"run": r["id"], "findings": n}, json_out,
             f"run {r['id']}: reported {n} finding{'s' if n != 1 else ''}" + ("; the human sees them on Today" if n else ""))


@schedule_app.command("findings")
def findings(json_out: JsonOpt = False) -> None:
    """Open findings from runs, waiting for the human on Today."""
    from orch.core import schedules
    cli, ws = _ctx()
    rows = [{"run": r["id"], "schedule": r["schedule"], **{k: f.get(k) for k in ("id", "title", "text", "ticket", "sha")}}
            for r, f in schedules.open_findings(ws)]
    lines = [f"{x['run']} {x['id']}  {x['schedule']}: {x['title']}"
             + (f"  → ticket \"{x['ticket']['title']}\"" if x["ticket"] else "") for x in rows]
    cli._out(rows, json_out, "\n".join(lines) or "no open findings")


@schedule_app.command("file")
def file_(run_id: str, finding: str, json_out: JsonOpt = False) -> None:
    """Human only: file a finding's proposed ticket in backlog."""
    from orch.actor import confirm_typed, require_human_terminal
    from orch.core import schedules
    from orch.errors import NotFoundError
    cli, ws = _ctx()
    require_human_terminal("filing a ticket from a schedule's finding")
    r = schedules.run(ws, run_id)
    f = next((x for x in (r or {}).get("findings") or [] if x.get("id") == finding), None)
    if f is None:
        raise NotFoundError(f"no finding {finding} in run {run_id}")
    t = f.get("ticket") or {"title": f["title"], "ask": f.get("text", "")}
    from orch.textsafe import lines as safe_lines
    typer.echo(f"Filing in backlog: {t['title']}")
    for line in safe_lines(t.get("ask", ""))[:40]:
        typer.echo(f"  | {line}")
    actor = confirm_typed(finding)
    tid = schedules.file_finding(ws, actor, run_id, finding, expected_sha=f.get("sha"))
    cli._out({"ticket": tid}, json_out, f"filed {tid}")


@schedule_app.command("dismiss")
def dismiss(run_id: str, finding: str, json_out: JsonOpt = False) -> None:
    """Human only: a finding needs nothing."""
    from orch.actor import human_actor, require_human_terminal
    from orch.core import schedules
    cli, ws = _ctx()
    require_human_terminal("dismissing a schedule's finding")
    schedules.dismiss_finding(ws, human_actor(finding), run_id, finding)
    cli._out({"run": run_id, "finding": finding, "state": "dismissed"}, json_out, f"{finding} dismissed")

