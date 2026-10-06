"""Schedules in Mission Control (docs/schedules.md): the page, and the human's answers. Served only while the
`schedules` default addon is enabled in this workspace; every POST passes the dashboard's cookie and same-origin
checks (auth_middleware), and orch.core.schedules refuses a process with an agent harness in its ancestry exactly as
the CLI does. Arming is bound to the hashes of the definition and the skill the page showed."""
from __future__ import annotations

from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Form, Request

from orch.core import schedules as sc
from orch.dashboard import schedules as dash
from orch.dashboard.reach import request_actor
from orch.dashboard.views import back, error_text, page, safe_next
from orch.errors import OrchError

router = APIRouter()
Next = Annotated[str, Form(alias="next")]
STATUS_ROLES = {"quiet": "neu", "finding": "you", "failed": "err", "skipped": "warn", "running": "info",
                "starting": "info"}
STATE_ROLES = {"armed": "ok", "draft": "neu", "paused": "warn", "changed": "warn", "invalid": "err"}


def _local(iso) -> datetime | None:
    try:
        return datetime.fromisoformat(iso) if isinstance(iso, str) else None
    except ValueError:
        return None


def findings_view(ws) -> list[dict]:
    """Open findings for Today and the page, newest run first; text the run wrote is plain text, never formatted."""
    out = []
    for r, f in sc.open_findings(ws):
        out.append({"run": r["id"], "schedule": r["schedule"], "name": r.get("name") or r["schedule"],
                    "started": _local(r.get("started")), "summary": r.get("summary") or "", "id": f["id"],
                    "title": f["title"], "text": f.get("text") or "", "ticket": f.get("ticket"), "sha": f.get("sha"),
                    "recurring": r.get("kind") == "recurring"})
    return out


def _row(ws, r: dict, now: datetime) -> dict:
    strip = []
    for x in reversed(r["recent"][:24]):
        strip.append({"status": x.get("status"), "at": _local(x.get("started")), "summary": x.get("summary") or x.get("reason") or ""})
    r["strip"] = strip
    r["role"] = STATE_ROLES.get(r["state"], "neu")
    r["next_at"] = _local(r["next"])
    r["last_role"] = STATUS_ROLES.get((r.get("last") or {}).get("status"), "neu")
    r["summary_text"] = sc.skill_summary(ws, r["skill"])
    return r


@router.get("/schedules")
def schedules_page(request: Request, sel: str = ""):
    ws = request.app.state.ws
    on = dash.addon_on(ws)
    now = datetime.now().astimezone()
    rows = [_row(ws, r, now) for r in sc.overview(ws, now)] if on else []
    chosen = next((r for r in rows if r["id"] == sel), rows[0] if rows else None)
    runs = [dict(x, role=STATUS_ROLES.get(x.get("status"), "neu"), at=_local(x.get("started")),
                 ended_at=_local(x.get("ended"))) for x in sc.runs(ws, limit=30)] if on else []
    for x in runs:
        x.pop("token", None)
    counts = {"armed": sum(1 for r in rows if r["state"] == "armed"),
              "today": sum(r["today"] for r in rows),
              "findings": len(sc.open_findings(ws)) if on else 0,
              "fix": sum(1 for r in rows if r["state"] in ("changed", "invalid"))}
    return page(request, "schedules.html", 200 if on else 404, nav="schedules", title="Schedules", addon_on=on,
                rows=rows, chosen=chosen, runs=runs, counts=counts, findings=findings_view(ws) if on else [],
                tmux=dash.factory_runner.available(), folder=str(sc.folder(ws).relative_to(ws.root)),
                status_roles=STATUS_ROLES)


def _answer(request: Request, next_url: str, action, success: str, default: str = "/schedules"):
    url = safe_next(next_url) or default
    ws = request.app.state.ws
    if not dash.addon_on(ws):
        return back(url, err="Schedules is an addon: enable it in Workspace & addons")
    try:
        result = action(ws)
    except OrchError as e:
        return back(url, err=error_text(e))
    return back(url, msg=success.format(result=result))


@router.post("/schedules/{sid}/arm")
def arm(request: Request, sid: str, def_sha: Annotated[str, Form()] = "", skill_sha: Annotated[str, Form()] = "",
        next_url: Next = ""):
    return _answer(request, next_url, lambda ws: sc.arm(ws, request_actor(request), sid, expected_def=def_sha,
                                                         expected_skill=skill_sha), f"{sid} armed")


@router.post("/schedules/{sid}/pause")
def pause(request: Request, sid: str, next_url: Next = ""):
    return _answer(request, next_url, lambda ws: sc.pause(ws, request_actor(request), sid), f"{sid} paused")


@router.post("/schedules/{sid}/run")
def run_now(request: Request, sid: str, next_url: Next = ""):
    return _answer(request, next_url, lambda ws: sc.request_run(ws, request_actor(request), sid),
                   f"{sid}: a run starts within a round")


@router.post("/schedules/runs/{rid}/{fid}/file")
def file_finding(request: Request, rid: str, fid: str, sha: Annotated[str, Form()] = "", next_url: Next = ""):
    return _answer(request, next_url, lambda ws: sc.file_finding(ws, request_actor(request), rid, fid,
                                                                  expected_sha=sha or None),
                   "filed {result} in backlog", default="/")


@router.post("/schedules/runs/{rid}/{fid}/dismiss")
def dismiss_finding(request: Request, rid: str, fid: str, next_url: Next = ""):
    return _answer(request, next_url, lambda ws: sc.dismiss_finding(ws, request_actor(request), rid, fid),
                   "finding dismissed", default="/")
