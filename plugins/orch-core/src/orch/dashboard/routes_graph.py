"""The Graph page: tickets, the files their commits changed and the links between tickets (orch.core.graph), drawn by
static/graph.js. The data is embedded in the page as a JSON block (the CSP allows no inline script); /graph.json
serves the same, and /graph/related the `orch related` text an agent reads for one ticket or file. All of it answers
404 until the human enables the `graph` default addon in this workspace (#167); `orch graph` stays core."""
from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from orch.core import graph as core_graph
from orch.core import related
from orch.dashboard.views import GRAPH_ADDON, addon_on, page
from orch.errors import OrchError

router = APIRouter()
VIEWS = ("code", "deps", "local")
OFF = "Graph is an addon: enable it in Workspace & addons"


def _off(request: Request) -> bool:
    return not addon_on(request.app.state.ws, GRAPH_ADDON)


def _data(ws) -> dict:
    from orch.dashboard.data.cards import ticket_moves
    data = core_graph.build(ws)
    moves = ticket_moves(ws)
    for n in data["nodes"]:
        move = moves.get(n["id"]) if n["kind"] == "ticket" else None
        if move and move["who"] == "you":
            n["needs"] = move["label"]
    return data


@router.get("/graph")
def graph_page(request: Request):
    if _off(request):
        return page(request, "error.html", 404, nav="graph", title="Graph", heading="Graph is off", message=OFF)
    ws = request.app.state.ws
    data = _data(ws)
    view = request.query_params.get("view")
    tickets = [n for n in data["nodes"] if n["kind"] == "ticket"]
    epics = [n for n in tickets if n.get("type") == "epic"]
    focus = request.query_params.get("t") or ""
    known = {n["id"].upper(): n["id"] for n in tickets}
    return page(request, "graph.html", nav="graph", title="Graph", g=data, epics=epics,
                view=view if view in VIEWS else "code", focus=known.get(focus.upper(), ""),
                ticket_count=len(tickets), file_count=len(data["nodes"]) - len(tickets),
                yours=[n for n in tickets if n.get("needs")])


@router.get("/graph.json")
def graph_json(request: Request):
    if _off(request):
        return JSONResponse({"error": OFF}, status_code=404)
    return JSONResponse(_data(request.app.state.ws))


@router.get("/graph/related")
def graph_related(request: Request):
    """{text, data} of `orch related` for ?t=<ticket> or ?p=<repo-qualified path>."""
    if _off(request):
        return JSONResponse({"error": OFF}, status_code=404)
    ws = request.app.state.ws
    ref, path = request.query_params.get("t"), request.query_params.get("p")
    if not ref and not path:
        return JSONResponse({"error": "pass t or p"}, status_code=400)
    try:
        data = related.related(ws, ref or None, [path] if path else [], limit=6)
    except OrchError as e:
        return JSONResponse({"error": e.message}, status_code=404)
    return JSONResponse({"text": related.render(data), "data": data})
