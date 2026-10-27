"""``orch stop [all|workspace]``, ``orch start [all]`` and ``orch services`` (the table of what runs here).

A person's job: an agent (``ORCH_GRANT`` / ``ORCH_SESSION`` set) is refused. Plain words, no network or vendor names."""

from __future__ import annotations

import json
import os
from collections.abc import Mapping, Sequence
from typing import Any, TextIO

from . import agents as A
from .plan import Plan, plan_start, plan_stop
from .units import Env, LifecycleError, Svc, discover, start_svc, stop_svc, stopped_by_orch

__all__ = ["main", "known_workspaces", "table"]

AGENT_ENV = ("ORCH_GRANT", "ORCH_SESSION")
USAGE = (
    "usage: orch stop              stop this workspace (run it inside a workspace folder)\n"
    "       orch stop all          stop every workspace and every background service on this computer\n"
    "       orch start [all]       start again what is installed\n"
    "       orch services          show what runs, what is stopped and what is installed\n"
    "       add --dry-run to see what would happen; --json on services for scripts\n"
)
STATE_WORDS = {"running": "running", "stopped": "stopped", "unknown": "unknown"}


def known_workspaces(e: Env) -> list[tuple[str, str | None]]:
    """``[(root, workspace id or None)]`` of the workspaces this computer serves or has set up."""
    found: dict[str, str | None] = {}
    try:
        doc = json.loads((e.state / "host" / "workspaces.json").read_text())
        for r in doc.get("workspaces") or []:
            if isinstance(r, dict) and isinstance(r.get("root"), str) and os.path.isabs(r["root"]):
                found.setdefault(
                    os.path.abspath(r["root"]),
                    r.get("workspace_id") if isinstance(r.get("workspace_id"), str) else None,
                )
    except (OSError, ValueError, AttributeError):
        pass
    from orch.install.state import Layout, load

    layout = Layout(e.env)
    if layout.file.exists():
        for w in load(layout).get("workspaces") or []:
            if isinstance(w, dict) and isinstance(w.get("dir"), str) and os.path.isabs(w["dir"]):
                found.setdefault(os.path.abspath(w["dir"]), None)
    return sorted(found.items())


def _here(env: Mapping[str, str], cwd: str | None) -> tuple[str, str | None] | None:
    """``(root, workspace id)`` of the workspace folder we are in, or ``None``."""
    from orch import discovery

    explicit = env.get("ORCH_WORKSPACE")
    try:
        found = discovery.locate(explicit or cwd or os.getcwd(), explicit=bool(explicit), v1=False)
    except OSError:
        return None
    if found is None or found.v1:
        return None
    wid = ((found.config or {}).get("workspace") or {}).get("id")
    return os.path.abspath(found.path), (wid.removeprefix("w_") if isinstance(wid, str) else None)


def table(e: Env, svcs: list[Svc]) -> None:
    e.say(f"{'service':<22}{'state':<14}notes")
    for s in svcs:
        if s.kind == "unsupported":
            e.say(f"{s.label:<22}{'-':<14}not available on this system")
        elif not s.installed:
            e.say(f"{s.label:<22}{'not installed':<14}")
        else:
            note = s.detail
            if s.state == "stopped" and s.name in stopped_by_orch(e.state):
                note = (note + "  " if note else "") + "stopped by orch stop, orch start brings it back"
            e.say(f"{s.label:<22}{STATE_WORDS.get(s.state, s.state):<14}{note}")
    ws = known_workspaces(e)
    if ws:
        host = next((s for s in svcs if s.key == "host"), None)
        up = host is not None and host.state == "running"
        counts = _agent_counts(e)
        e.say("")
        e.say(f"workspaces on this computer ({len(ws)}):")
        for root, wid in ws:
            n = counts.get(wid, 0) if wid else 0
            state = "served" if up else "not served (the host is stopped)"
            e.say(f"  {root}   {state}" + (f", {n} agent{'s' if n != 1 else ''} running" if n else ""))


def _agent_counts(e: Env) -> dict[str, int]:
    out: dict[str, int] = {}
    for r in A.live_runs(e.state, None):
        out[r["workspace_id"]] = out.get(r["workspace_id"], 0) + 1
    return out


def _svcs_json(svcs: list[Svc]) -> list[dict[str, Any]]:
    return [
        {
            "service": s.key,
            "installed": s.installed and s.kind != "unsupported",
            "state": s.state if s.installed else None,
            "address": s.detail or None,
        }
        for s in svcs
    ]


def _run_steps(e: Env, plan: Plan, svcs: list[Svc], wid: str | None, root: str | None) -> int:
    by = {s.key: s for s in svcs}
    failed = 0
    for step in plan.steps:
        if step.action == "agents":
            runs = A.live_runs(e.state, wid) if wid else []
            if not runs:
                e.say("  no agents running in this workspace")
                continue
            ended, notes = A.end_agents(e, runs, A.runner_pid(e, by.get("runner")))
            e.say(f"  agents of this workspace: {ended} of {len(runs)} ended")
            for n in notes:
                e.say(f"    {n}")
            failed += 1 if notes else 0
            continue
        s = by[step.key]
        before = s.state
        try:
            word = stop_svc(e, s) if step.action == "stop" else start_svc(e, s)
        except LifecycleError as ex:
            e.say(f"  FAILED    {s.label}: {ex}")
            failed += 1
            continue
        extra = ""
        if step.action == "stop" and step.key == "runner" and before == "running":
            extra = " (its agents ended first)"
        e.say(f"  {word:<10}{s.label}{extra}")
    return failed


def main(
    argv: Sequence[str],
    *,
    env: Mapping[str, str],
    out: TextIO,
    err: TextIO,
    cmd: str,
    e: Env | None = None,
    cwd: str | None = None,
) -> int:
    """``cmd`` is the word the person typed: stop | start | services."""
    if any(env.get(k) for k in AGENT_ENV):
        err.write(f"orch {cmd}: starting and stopping services is a person's job; an agent never does it\n")
        return 3
    args = [a for a in argv if not a.startswith("--")]
    flags = {a for a in argv if a.startswith("--")}
    scope_word = args[0] if args else ""
    if (
        flags - {"--dry-run", "--json"}
        or len(args) > 1
        or (cmd == "stop" and scope_word not in ("", "all", "workspace"))
        or (cmd == "start" and scope_word not in ("", "all"))
        or (cmd == "services" and args)
    ):
        err.write(USAGE)
        return 2
    e = e or Env(env=env, out=out, err=err)
    svcs = discover(e)
    if cmd == "services":
        if "--json" in flags:
            e.say(
                json.dumps({"services": _svcs_json(svcs), "workspaces": [r for r, _ in known_workspaces(e)]}, indent=2)
            )
        else:
            table(e, svcs)
        return 0
    dry = "--dry-run" in flags
    here = _here(env, cwd)
    marked = stopped_by_orch(e.state)
    if cmd == "stop":
        if scope_word == "all":
            plan = plan_stop("all", svcs, marked)
        else:
            if here is None:
                err.write(
                    "orch stop: this folder is not an orch workspace, so there is nothing of it to stop.\n"
                    "  to stop every workspace and background service on this computer: orch stop all\n"
                    "  to see what runs: orch services\n"
                )
                return 1
            others = [r for r, w in known_workspaces(e) if r != here[0] and (here[1] is None or w != here[1])]
            plan = plan_stop("workspace", svcs, marked, len(others))
        title = (
            "Stopping everything orch runs on this computer"
            if plan.scope == "all"
            else f"Stopping the workspace {here[0] if here else ''}"
        )
    else:
        scope = "all" if scope_word == "all" or here is None else "workspace"
        plan = plan_start(scope, svcs)
        title = "Starting everything that is installed" if scope == "all" else "Starting the services of this workspace"
    if not plan.steps and not plan.notes:
        e.say(
            "Nothing to do: "
            + ("everything is stopped already." if cmd == "stop" else "nothing is installed to start.")
        )
        return 0
    e.say(title + (" (dry run, nothing is changed):" if dry else ":"))
    if dry:
        for st in plan.steps:
            if st.action == "agents":
                e.say("  would end the running agents of this workspace")
            else:
                s = next(x for x in svcs if x.key == st.key)
                e.say(f"  would {st.action:<5} {s.label}")
        for n in plan.notes:
            e.say(n)
        return 0
    failed = _run_steps(e, plan, svcs, here[1] if here else None, here[0] if here else None)
    for n in plan.notes:
        e.say(n)
    if cmd == "stop" and failed == 0 and plan.scope == "all":
        e.say(
            "Everything is stopped, also after a restart of this computer. Nothing was removed.\n"
            "Bring it back: orch start all"
        )
    elif cmd == "stop" and failed == 0 and any(s.action == "stop" for s in plan.steps):
        e.say("Stopped, also after a restart of this computer. Bring it back: orch start")
    e.say("")
    table(e, discover(e))
    return 1 if failed else 0
