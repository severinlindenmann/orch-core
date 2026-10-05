"""GET /guide: a short, honest explainer of orch and Mission Control (why, what, how). The copy is static and human-only
like every page (behind the dashboard token); the one dynamic part is the `guide.section` slot, where an enabled, trusted
addon may add its own section (rendered by core from widgets, escaped like any addon content). It reads nothing from
the workspace."""
from __future__ import annotations

from fastapi import APIRouter, Request

from orch.dashboard.views import page

router = APIRouter()


def _sections(request: Request):
    """(groups, phone_covered): the guide sections of enabled, trusted addons, and whether one of them is a phone
    companion (manifest `remote_humans`), which then replaces core's generic phone paragraph."""
    runtime = getattr(request.app.state, "addons", None)
    if runtime is None:
        return [], False
    groups = runtime.slot("guide.section")
    names = {g.addon for g in groups if g.widgets}
    try:
        covered = any(la.name in names and la.manifest.remote_humans for la in runtime.registry)
    except Exception:
        covered = False
    return groups, covered


@router.get("/guide")
def guide_page(request: Request):
    groups, covered = _sections(request)
    return page(request, "guide.html", nav="guide", title="How it works", guide_groups=groups, phone_covered=covered)
