"""Activity: the agents holding tickets right now, pinned above the timeline feed (spec §4.6).

Replaces the separate Agents and Timeline pages; their old URLs redirect here, query kept.
"""
from __future__ import annotations

from urllib.parse import urlencode

from fastapi import APIRouter, Request
from fastapi.responses import PlainTextResponse, RedirectResponse

from orch.core import query, store
from orch.core.events import read_events
from orch.dashboard.data.agents import STATUS_LABELS, agent_rows, recent_sessions
from orch.dashboard.data.timeline import CATEGORIES, CATEGORY_LABELS, PAGE_SIZE, timeline, to_markdown
from orch.dashboard.views import page

router = APIRouter()

_STATUSES = ("working", "waiting", "stale")


def _timeline_params(request: Request) -> tuple[str, int | None]:
    category = request.query_params.get("category", "all")
    category = category if category in CATEGORIES else "all"
    raw_before = request.query_params.get("before")
    before = None
    if raw_before is not None:
        try:
            parsed = int(raw_before)
        except ValueError:
            parsed = None
        if parsed is not None and parsed >= 1:
            before = parsed
    return category, before


def _with_query(path: str, request: Request, fragment: str = "") -> str:
    query = request.url.query
    return path + (f"?{query}" if query else "") + fragment


@router.get("/agents")
def agents_redirect(request: Request):
    return RedirectResponse(_with_query("/activity", request), status_code=303)


@router.get("/timeline")
def timeline_redirect(request: Request):
    return RedirectResponse(_with_query("/activity", request, "#timeline"), status_code=303)


@router.get("/timeline.md")
def timeline_markdown_redirect(request: Request):
    return RedirectResponse(_with_query("/activity.md", request), status_code=303)


@router.get("/activity")
def activity(request: Request):
    ws = request.app.state.ws
    status = request.query_params.get("status", "all")
    status = status if status in _STATUSES else "all"
    category, before = _timeline_params(request)
    all_events = read_events(ws)  # read the log once, share it between agent_rows and recent_sessions
    rows = agent_rows(ws, events=all_events)
    counts = {"all": len(rows)} | {s: sum(1 for r in rows if r.status == s) for s in _STATUSES}
    shown = rows if status == "all" else [r for r in rows if r.status == status]
    tl = timeline(ws, category=category, before=before, limit=PAGE_SIZE)
    # Each ticket key in the timeline shows its title too (cut by CSS), from the scan this request already did.
    titles = {e.id: (e.meta or {}).get("title") or "" for e in store.scan(ws)}

    def link(*, status_: str = status, category_: str = category, before_: int | None = None,
             fragment: str = "") -> str:
        """A link to this page that changes one filter and keeps the other."""
        params = [(k, v) for k, v in (("status", status_), ("category", category_)) if v != "all"]
        if before_:
            params.append(("before", before_))
        return "/activity" + ("?" + urlencode(params) if params else "") + fragment

    return page(request, "activity.html", nav="activity", title="Activity", waiting=query.waiting(ws, events=all_events),
                rows=shown, status=status, counts=counts, recent=recent_sessions(ws, events=all_events),
                labels=STATUS_LABELS, tl=tl, category=category, categories=CATEGORIES,
                category_labels=CATEGORY_LABELS, before=before, link=link, titles=titles)


@router.get("/activity.md")
def activity_markdown(request: Request):
    ws = request.app.state.ws
    category, before = _timeline_params(request)
    tl = timeline(ws, category=category, before=before, limit=PAGE_SIZE)
    return PlainTextResponse(to_markdown(tl), media_type="text/markdown; charset=utf-8")
