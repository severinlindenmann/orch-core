"""GET /guide: a short, honest explainer of orch and Mission Control (why, what, how). Static copy, human-only like every
page (behind the dashboard token); it reads nothing from the workspace."""
from __future__ import annotations

from fastapi import APIRouter, Request

from orch.dashboard.views import page

router = APIRouter()


@router.get("/guide")
def guide_page(request: Request):
    return page(request, "guide.html", nav="guide", title="How it works")
