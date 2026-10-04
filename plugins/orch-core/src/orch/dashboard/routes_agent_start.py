"""POST /t/{ref}/agent/start: open a terminal running an agent on the ticket (spec §7).

Human-only by construction: it needs the dashboard's token cookie and passes the same Origin
check as every other POST (auth_middleware). Nothing starts an agent but this explicit click.
"""
from __future__ import annotations

import os
import sys
from typing import Annotated

from fastapi import APIRouter, Form, Request

from orch.clock import now as clock_now
from orch.core import query, store
from orch.core.ops import Ops
from orch.dashboard import launch, terminals
from orch.dashboard.data import agent_start
from orch.dashboard.data.agents import agent_rows
from orch.dashboard.views import HUMAN, back, error_text, safe_next
from orch.errors import OrchError

router = APIRouter()


@router.post("/t/{ref}/agent/start")
def start_agent(request: Request, ref: str, mode: Annotated[str, Form()], harness: Annotated[str, Form()],
                next_url: Annotated[str, Form(alias="next")] = "", where: Annotated[str, Form()] = ""):
    ws = request.app.state.ws
    try:
        _, t = store.load(ws, ref)
    except OrchError as e:
        return back(safe_next(next_url) or "/", err=error_text(e))
    url = safe_next(next_url) or f"/t/{t.id}"
    try:
        settings = launch.load_settings()  # per user only, never the agent-writable workspace config
        terminal = launch.choose(os.environ, settings["terminal"], sys.platform)
        if where == "tmux":  # the "Open in Mission Control" button, whatever the default terminal is
            terminal = "tmux"
        if terminal == "tmux" and not terminals.enabled(ws, request):  # also "terminal": "tmux" in launch.json
            return back(url, err=terminals.why_off(ws))
        allowed = terminals.settings(ws.root)["harness"]
        if terminal == "tmux" and harness != allowed:
            return back(url, err=f"Terminals runs {agent_start.HARNESS_LABELS.get(allowed, allowed)} only for now: "
                                 f"pick it as Harness, or use Open in terminal")
        if terminal == "none":
            return back(url, err="Open in terminal is turned off")
        at = clock_now()
        needs = query.needs_you(ws)
        s = agent_start.suggest(ws, t, needs_items=needs, rows=agent_rows(ws, now=at, needs=needs), now=at,
                                settings=settings, request=request)
        if s is None:
            return back(url, err=f"{t.id} is done; there is nothing to start")
        if s["disabled"]:
            return back(url, err=s["disabled"])
        if mode not in [m["value"] for m in s["modes"]]:  # e.g. work on an epic: only what the panel offers
            return back(url, err=f"{mode} is not offered for {t.id}")
        _, argv, _ = agent_start.build(ws, t.id, mode, harness, pr=s["pr"], settings=settings)
        launch.preflight(terminal, settings)  # cheap launcher checks before the claim is touched
        if s["warning"]:  # a stale claim: release it so the new agent can claim the ticket
            Ops(ws, HUMAN).release(t.id)
        name = terminals.free_name(ws, t.id) if terminal == "tmux" else t.id
        msg = launch.start(ws, t.id, argv, terminal=terminal, name=name, harness=harness, settings=settings)
    except OrchError as e:
        return back(url, err=error_text(e))
    if terminal == "tmux":
        return back(f"/terminals/{name}", msg=msg)
    return back(url, msg=msg)
