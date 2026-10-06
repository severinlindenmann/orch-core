"""Quick tasks in Mission Control (orch.core.quick): the list with its add box, one task's page, the human's actions on
them, and Start agent into Mission Control's Terminals or the human's own terminal. Every write goes through
QuickOps, so the dashboard keeps the CLI's rules. All of it answers 404 until the human enables the `quick-tasks`
default addon in this workspace, as Graph does with `graph`."""
from __future__ import annotations

import os
import sys
from typing import Annotated

from fastapi import APIRouter, Form, Request

from orch.clock import now, parse_stamp
from orch.core import quick
from orch.core.quick import QuickOps
from orch.dashboard.reach import request_actor
from orch.dashboard.views import back, error_text, page, safe_next
from orch.errors import OrchError

router = APIRouter()
OFF = "Quick tasks is an addon: enable it in Workspace & addons"
# What a started agent is told: the same steps the orch-tickets skill gives, with the task's key only (never its text)
PROMPT = ("Work on quick task {key} (orch-tickets skill, Quick tasks): claim it with `orch quick claim {key}`, do it, "
          "commit as `{key} <summary>` and close it with `orch quick done {key} -m \"<what you did>\"`. If it outgrows "
          "the size limit, stop and tell me.")
FILTERS = (("open", "Open"), ("outgrew", "Outgrew"), ("done", "Done and moved"), ("all", "All"))
Next = Annotated[str, Form(alias="next")]


def _ops(request: Request) -> QuickOps:
    return QuickOps(request.app.state.ws, request_actor(request))


def _run(request: Request, url: str, action, success) -> object:
    url = safe_next(url) or "/quick"
    try:
        result = action()
    except OrchError as e:
        return back(url, err=error_text(e))
    return back(url, msg=success(result) if callable(success) else success)


def _off(request: Request):
    """The 404 page while the `quick-tasks` addon is off, else None."""
    if quick.enabled(request.app.state.ws):
        return None
    return page(request, "error.html", 404, nav="quick", title="Quick tasks", heading="Quick tasks are off", message=OFF)


def _today(stamp: str | None) -> bool:
    try:
        return parse_stamp(str(stamp)).date() == now().date()
    except ValueError:
        return False


def summary(ws) -> dict:
    """Counts for the menu and the Today tile: open, outgrown, done today."""
    cfg = quick.settings(ws)
    if not cfg["enabled"]:
        return {"enabled": False, "open": 0, "outgrew": 0, "done_today": 0}
    tasks = quick.all_tasks(ws)
    return {"enabled": True,
            "open": sum(t["status"] == "open" for t in tasks),
            "outgrew": sum(t["status"] == "open" and bool(t.get("outgrew")) for t in tasks),
            "done_today": sum(t["status"] == "done" and _today((t.get("done") or {}).get("at")) for t in tasks)}


def _who(by: str | None) -> str:
    by = str(by or "")
    if by.startswith("human:"):
        return "you"
    if by.startswith("agent:"):
        return by.split(":")[1]
    return by or "someone"


def _row(ws, t: dict, cfg: dict) -> dict:
    v = quick.view(ws, t, cfg)
    if v["status"] == "done":
        who, at = f"done by {_who((v.get('done') or {}).get('by'))}", (v.get("done") or {}).get("at")
    elif v["status"] == "moved":
        who, at = "moved to a ticket", None
    elif v["status"] == "dropped":
        who, at = "dropped", None
    else:
        who, at = f"added by {_who((v.get('added') or {}).get('by'))}", (v.get("added") or {}).get("at")
    return {**v, "who": who, "at": at}


@router.get("/quick")
def quick_page(request: Request, f: str = "open"):
    if (off := _off(request)) is not None:
        return off
    ws = request.app.state.ws
    cfg = quick.settings(ws)
    f = f if f in dict(FILTERS) else "open"
    tasks = quick.all_tasks(ws)
    views = {
        "open": sorted([t for t in tasks if t["status"] == "open"], key=lambda t: (bool(t.get("outgrew")), -quick._num(t["id"]))),
        "outgrew": [t for t in tasks if t["status"] == "open" and t.get("outgrew")],
        "done": [t for t in tasks if t["status"] in ("done", "moved")],
        "all": tasks,
    }
    rows = [_row(ws, t, cfg) for t in views[f]]
    filters = [(key, label, len(views[key])) for key, label in FILTERS]
    return page(request, "quick.html", nav="quick", title="Quick tasks", cfg=cfg, rows=rows, f=f, filters=filters,
                s=summary(ws), prefix_q=cfg["prefix"], start=start_options(ws, request))


@router.get("/quick/{qid}")
def quick_task_page(request: Request, qid: str):
    if (off := _off(request)) is not None:
        return off
    ws = request.app.state.ws
    cfg = quick.settings(ws)
    try:
        t = quick.load(ws, qid)
    except OrchError as e:
        return back("/quick", err=error_text(e))
    from orch.core.events import read_events
    timeline = [e for e in read_events(ws) if (e.data or {}).get("quick") == t["id"]]
    from orch.core import artifacts as art
    files = [{**a, "image": bool(a.get("name")) and art.is_image(a["name"]),
              "href": f"/a/{t['id']}/{a['name']}" if a.get("name") else a.get("url")} for a in t.get("artifacts") or []]
    return page(request, "quick_task.html", nav="quick", title=t["id"], t=_row(ws, t, cfg), cfg=cfg, files=files,
                timeline=timeline, who=_who, start=start_options(ws, request), prompt=PROMPT.format(key=t["id"]))


@router.post("/quick/add")
def add(request: Request, title: Annotated[str, Form()] = "", area: Annotated[str, Form()] = "", next_url: Next = ""):
    return _run(request, next_url, lambda: _ops(request).add(title, area or None), lambda t: f"added {t['id']}")


@router.post("/quick/{qid}/done")
def done(request: Request, qid: str, note: Annotated[str, Form()] = "", next_url: Next = ""):
    return _run(request, next_url, lambda: _ops(request).done(qid, note or None), lambda t: f"{t['id']} done")


@router.post("/quick/{qid}/reopen")
def reopen(request: Request, qid: str, note: Annotated[str, Form()] = "", next_url: Next = ""):
    return _run(request, next_url, lambda: _ops(request).reopen(qid, note or None), lambda t: f"{t['id']} is open again")


@router.post("/quick/{qid}/release")
def release(request: Request, qid: str, next_url: Next = ""):
    return _run(request, next_url, lambda: _ops(request).release(qid), lambda t: f"{t['id']} released")


@router.post("/quick/{qid}/promote")
def promote(request: Request, qid: str, next_url: Next = ""):
    return _run(request, next_url, lambda: _ops(request).promote(qid),
                lambda r: f"{r[0]['id']} is now {r[1].id} in backlog")


@router.post("/quick/{qid}/drop")
def drop(request: Request, qid: str, next_url: Next = ""):
    return _run(request, next_url, lambda: _ops(request).drop(qid), lambda t: f"{t['id']} dropped")


def start_options(ws, request) -> dict:
    """What Start agent can do here: open in Mission Control (the `terminals` addon on, tmux installed, a local
    request) and/or in the human's own terminal (launch.json does not turn it off)."""
    from orch.dashboard import launch, terminals
    try:
        own = launch.load_settings().get("terminal") != "none"
    except OrchError:
        own = False
    return {"tmux": terminals.enabled(ws, request), "terminal": own}


@router.post("/quick/{qid}/agent/start")
def start_agent(request: Request, qid: str, where: Annotated[str, Form()] = "", next_url: Next = ""):
    """Open an agent on the quick task: in Mission Control's Terminals (`where=tmux`) or the default terminal. Only an
    open task nobody holds; the agent claims it itself with `orch quick claim`."""
    from orch.dashboard import launch, terminals
    from orch.dashboard.data import agent_start
    ws = request.app.state.ws
    url = safe_next(next_url) or f"/quick/{qid}"
    try:
        cfg = quick.settings(ws)
        if not cfg["enabled"]:
            return back(url, err=OFF)
        t = quick.load(ws, qid)
        if t["status"] != "open" or t.get("outgrew"):
            return back(url, err=f"{t['id']} is {'outgrown' if t.get('outgrew') else t['status']}; nothing to start")
        held = quick.active_claim(t, cfg["claim_minutes"])
        if held:
            return back(url, err=f"{t['id']} is already claimed by {held.get('harness')}")
        settings = launch.load_settings()  # per user only, never the agent-writable workspace config
        terminal = "tmux" if where == "tmux" else launch.choose(os.environ, settings["terminal"], sys.platform)
        if terminal == "none":
            return back(url, err="Open in terminal is turned off")
        if terminal == "tmux" and not terminals.enabled(ws, request):
            return back(url, err=terminals.why_off(ws))
        harness = terminals.settings(ws.root)["harness"] if terminal == "tmux" else agent_start.default_harness(ws, settings)
        known = agent_start.harnesses(ws, settings)
        if harness not in known:
            return back(url, err="no agent CLI is configured for Start agent")
        prompt = PROMPT.format(key=t["id"])
        argv = [part.replace("{prompt}", prompt) for part in known[harness]]
        launch.preflight(terminal, settings)
        name = terminals.free_name(ws, t["id"]) if terminal == "tmux" else t["id"]
        msg = launch.start(ws, t["id"], argv, terminal=terminal, name=name, harness=harness, settings=settings)
    except OrchError as e:
        return back(url, err=error_text(e))
    if terminal == "tmux":
        return back(f"/terminals/{name}", msg=msg)
    return back(url, msg=msg)
