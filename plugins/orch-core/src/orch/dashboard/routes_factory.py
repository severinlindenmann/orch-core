"""AI Factory and Dark AI Factory on the dashboard (phase 5): the factory list and one factory's run view, and Retry release (phase 6). Otherwise read only:
every step, state and count comes from records orch keeps (the signed charter and ledger, the events, the tickets'
own state, the runner's markers); the actions on the page are the existing ones (permission cards, the Ready verdict,
pausing the epic). Absent while `factory.enabled` is off."""
from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Form, Request

from orch.dashboard.data import factory as factory_data
from orch.dashboard.routes_ticket import load_or_error
from orch.dashboard.views import HUMAN, back, error_text, page
from orch.errors import OrchError, UsageError

router = APIRouter()


def _not_found(request: Request, message: str):
    return page(request, "error.html", 404, nav="factory", title="Not found", heading="Not found", message=message)


@router.get("/factory")
def factory_list(request: Request):
    rows = factory_data.factory_list(request.app.state.ws)
    if rows is None:
        return _not_found(request, "AI Factory is switched off in this workspace")
    counts = {"all": len(rows), "working": sum(r["live"] for r in rows),
              "you": sum(r["state"] in factory_data.NEEDS_YOU for r in rows)}
    return page(request, "factory.html", nav="factory", title="Factories", rows=rows, counts=counts,
                steps=factory_data.STEPS)


@router.get("/factory/{ref}")
def factory_run(request: Request, ref: str):
    ws, _, epic, error = load_or_error(request, ref)
    if error:
        return error
    run = factory_data.run_view(ws, epic)
    if run is None:
        return _not_found(request, f"{epic.id} is not an AI Factory epic, or AI Factory is switched off")
    return page(request, "factory_run.html", nav="factory", title=f"{run['name']} {epic.id}", run=run,
                steps=factory_data.STEPS)


@router.post("/factory/{ref}/release/retry")
def release_retry(request: Request, ref: str, stage: Annotated[str, Form()] = "", unit: Annotated[str, Form()] = ""):
    """Retry release (phase 6): one more attempt of one failed or unknown stage, or a fresh check of the branches
    after a sensitive-path stop. Yours only: orch.core.factory_release.retry refuses an agent and any process under an
    agent harness; it runs nothing itself, the runner's next round does."""
    from orch.core import factory_release, permits, store
    ws = request.app.state.ws
    try:
        eid = store.resolve(ws, ref).id
    except OrchError as e:
        return back("/factory", err=error_text(e))
    url = f"/factory/{eid}"
    try:
        if not permits.enabled(ws):
            raise UsageError("AI Factory is switched off in this workspace")
        text = factory_release.retry(ws, HUMAN, eid, stage, unit)
    except OrchError as e:
        return back(url, err=error_text(e))
    return back(url, msg=text)


@router.post("/factory/{ref}/reopen")
def reopen(request: Request, ref: str, reason: Annotated[str, Form()] = ""):
    """Reopen an epic the runner closed by itself under its charter: the existing reopen (Ops.reopen), yours only
    (it refuses an agent and any process under an agent harness). Its children stay done."""
    from orch.core import factory_close, store
    from orch.core.ops import Ops
    ws = request.app.state.ws
    try:
        epic = store.read_ticket(store.resolve(ws, ref).path)
    except OrchError as e:
        return back("/factory", err=error_text(e))
    url = f"/factory/{epic.id}"
    try:
        if not factory_close.closed_by_charter(ws, epic):
            raise UsageError(f"{epic.id} was not closed by its charter: reopen it with orch reopen in a terminal")
        Ops(ws, HUMAN).reopen(epic.id, reason)
    except OrchError as e:
        return back(url, err=error_text(e))
    return back(url, msg=f"reopened {epic.id}")
