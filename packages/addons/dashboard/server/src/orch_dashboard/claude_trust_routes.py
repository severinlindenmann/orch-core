"""The Claude Code consent switch of the Settings page.

* ``GET  /api/w/{ws}/claude-trust`` whether the person's consent is on for this workspace's folder, and whether it
  can be offered (and why not).
* ``POST /api/w/{ws}/claude-trust`` `{allow_parent}` grants the consent (`claude_folder.grant_consent`); the runner
  pre-trusts each ticket folder itself, so nothing else is written here.

Both need a full local session (the same gate as the trusted browsers) and the workspace OWNER role (host roster).
"""

import os
from pathlib import Path

from fastapi import Depends, Request
from orch.runner import claude_trust
from orch.setup import claude_folder
from pydantic import BaseModel, ConfigDict

from . import adapters
from .errors import fail, step_up_response
from .policy import LOOPBACK
from .reads import workspace


class Grant(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    allow_parent: bool


def register(app, gateway, auth):
    def local_only(request: Request):
        """The switch changes what Claude Code may do: a full local session only (a switch one never gets here)."""
        token = request.state.token
        if auth.login_kind(token) != "local" or auth.listener_of(token) != LOOPBACK:
            return step_up_response(False)
        return None

    def owner_only(ws):
        me = app.state.person_of(ws)
        if not any(m.person == me and m.role == "owner" for m in adapters.members(gateway.call(ws, "member.list"))):
            raise fail("role.denied", "Only an owner decides what Claude Code may open on this computer.", exit=3)

    @app.get("/api/w/{ws}/claude-trust")
    def status(request: Request, ws: str = Depends(workspace)):
        denied = local_only(request)
        if denied is not None:
            return denied
        owner_only(ws)
        project = gateway.workspace_folder(ws)[0]
        root = Path(os.path.realpath(project))
        env = os.environ
        config = claude_folder.config_state(env, root)
        return {
            "available": True,
            # on = the person agreed AND Claude Code's settings file is really there (never "on" while it is missing)
            "on": bool(claude_trust._consented(str(root))) and config == "ready",
            "config": config,
            "offered": bool(claude_folder.offered(env, root, allow_parent=True)),
            "why_not": claude_folder.refusal(env, root, allow_parent=True) or "",
            "parent_config": str(claude_folder.parent_config(root, env) or ""),
        }

    @app.post("/api/w/{ws}/claude-trust")
    def grant(body: Grant, request: Request, ws: str = Depends(workspace)):
        denied = local_only(request)
        if denied is not None:
            return denied
        owner_only(ws)
        project = gateway.workspace_folder(ws)[0]
        reason = claude_folder.grant_consent(os.environ, project, allow_parent=body.allow_parent)
        if reason:
            raise fail("invalid.input", reason, exit=1)
        if claude_folder.config_state(os.environ) == "missing" and not claude_folder.trust_folder(
            os.environ, project, allow_parent=body.allow_parent
        ):
            raise fail("invalid.input", "Claude Code is not set up yet: start it once, then try again.", exit=1)
        return {"on": True}
