"""Quick tasks in Mission Control (orch.core.quick): the list with its add box, one task's page, and the human's
actions on them. Every write goes through QuickOps, so the dashboard keeps the CLI's rules; the switch is the
human's own (never from a paired device)."""
from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Form, Request

from orch.clock import now, parse_stamp
from orch.core import quick
from orch.core.quick import QuickOps
from orch.dashboard.reach import request_actor
from orch.dashboard.views import back, error_text, page, safe_next
from orch.errors import OrchError

router = APIRouter()
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
                s=summary(ws), prefix_q=cfg["prefix"])


@router.get("/quick/{qid}")
def quick_task_page(request: Request, qid: str):
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
                timeline=timeline, who=_who)


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


@router.post("/quick/settings")
def settings(request: Request, enabled: Annotated[str, Form()] = "", agents_add: Annotated[str, Form()] = "",
             max_commits: Annotated[str, Form()] = "", max_files: Annotated[str, Form()] = "", next_url: Next = ""):
    def _int(v: str) -> int | None:
        try:
            return int(v) if v.strip() else None
        except ValueError:
            return -1  # refused by set_settings with the range it allows
    return _run(request, next_url,
                lambda: quick.set_settings(request.app.state.ws, request_actor(request), enabled=enabled == "1",
                                           agents_add=agents_add == "1", max_commits=_int(max_commits),
                                           max_files=_int(max_files)),
                lambda cfg: "quick tasks are on" if cfg["enabled"] else "quick tasks are off")
