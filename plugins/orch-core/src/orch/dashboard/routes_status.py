"""`GET /__orch/status` (#167): who serves this port, for the other workspaces' switchers. Read-only, no ticket
content, no token (auth_middleware lets it through), and only for a local request: anything else gets a 404."""
from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, PlainTextResponse

from orch.dashboard import switcher
from orch.dashboard.reach import reach

router = APIRouter()


@router.get(switcher.STATUS_PATH)
def orch_status(request: Request):
    if reach(request).kind != "local":
        return PlainTextResponse("not found", status_code=404)
    state = request.app.state
    try:
        addons = list(state.addons.registry)
    except Exception:  # a broken addon registry only empties the list
        addons = []
    try:
        body = switcher.status(state.ws, started=state.started, addons=addons)
    except OSError:  # the workspace id could not be read or written
        return PlainTextResponse("status unavailable", status_code=503)
    return JSONResponse(body, headers={"Cache-Control": "no-store"})
