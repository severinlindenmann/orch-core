from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import PlainTextResponse

from orch.dashboard.data.metrics import (DAYS_LABELS, STATUS_LABELS, WINDOWS, report, report_markdown,
                                         status_distribution)
from orch.dashboard.views import page

router = APIRouter()


def _days(request: Request) -> int:
    raw = request.query_params.get("days")
    try:
        parsed = int(raw)
    except (TypeError, ValueError):
        return 28
    return parsed if parsed in WINDOWS else 28


@router.get("/reports")
def reports_page(request: Request):
    ws = request.app.state.ws
    days = _days(request)
    r = report(ws, days=days)
    dist = status_distribution(ws)
    return page(request, "reports.html", nav="reports", title="Reports", r=r, days=days,
                windows=WINDOWS, days_labels=DAYS_LABELS, dist=dist, dist_total=sum(d["n"] for d in dist),
                status_labels=STATUS_LABELS)


@router.get("/reports.md")
def reports_markdown(request: Request):
    ws = request.app.state.ws
    days = _days(request)
    r = report(ws, days=days)
    return PlainTextResponse(report_markdown(r), media_type="text/markdown; charset=utf-8")
