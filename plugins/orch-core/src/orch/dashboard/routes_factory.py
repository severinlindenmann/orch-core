"""AI Factory and Dark AI Factory on the dashboard (phase 5): the factory list and one factory's run view. Read only:
every step, state and count comes from records orch keeps (the signed charter and ledger, the events, the tickets'
own state, the runner's markers); the actions on the page are the existing ones (permission cards, the Ready verdict,
pausing the epic). Absent while `factory.enabled` is off."""
from __future__ import annotations

from fastapi import APIRouter, Request

from orch.dashboard.data import factory as factory_data
from orch.dashboard.routes_ticket import load_or_error
from orch.dashboard.views import page

router = APIRouter()


def _not_found(request: Request, message: str):
    return page(request, "error.html", 404, nav="factory", title="Not found", heading="Not found", message=message)


@router.get("/factory")
def factory_list(request: Request):
    rows = factory_data.factory_list(request.app.state.ws)
    if rows is None:
        return _not_found(request, "AI Factory is switched off in this workspace")
    counts = {"all": len(rows), "working": sum(r["state"] == "working" for r in rows),
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
