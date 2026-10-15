"""Reduced workspace settings reads (M4, #306): a closed list of typed routes, one fixed host op behind each.

The server forwards what the host gives this person. It never widens it: there is no second read to fill a gap, no
default of its own and no field the host did not send. A host without `settings/1` answers `available: false` and the
page says so. Writes are hand-offs (handoff.py); nothing here signs or changes anything.
"""

from typing import Annotated

from fastapi import Depends, FastAPI, Query
from pydantic import BaseModel, ConfigDict

from . import adapters
from .gateway import Gateway
from .reads import workspace


class GrantsQuery(BaseModel):
    model_config = ConfigDict(extra="forbid")
    active: bool = True


def register(app: FastAPI, gateway: Gateway):
    @app.get("/api/w/{ws}/settings")
    def settings(ws: str = Depends(workspace)):
        link = gateway.enabled(ws)
        if not link.supports("workspace.settings", "settings/1"):
            return adapters.settings.unavailable()
        first = link.call("workspace.settings", {})
        more, page, extra = [], first.get("next_page"), False
        while page:
            if len(more) + 1 >= adapters.settings.MAX_REPO_PAGES:
                extra = True  # more repos than the page lists: say so, never present a short list as complete
                break
            data = link.call("workspace.settings", {"page": page})
            more.append(data)
            page = data.get("next_page")
        return adapters.settings.settings(first, more, extra)

    @app.get("/api/w/{ws}/members")
    def members(ws: str = Depends(workspace)):
        return adapters.settings.roster(gateway.call(ws, "member.list"))

    @app.get("/api/w/{ws}/readers")
    def readers(ws: str = Depends(workspace)):
        # H12d-2: every current device's reader level and the workspace's `required_reader`. A host without
        # `reader/1` answers `available: false` and the page says so.
        link = gateway.enabled(ws)
        if not link.supports("reader.list", "reader/1"):
            return adapters.settings.readers_unavailable()
        return adapters.settings.readers(link.call("reader.list", {}))

    @app.get("/api/w/{ws}/grants")
    def grants(query: Annotated[GrantsQuery, Query()], ws: str = Depends(workspace)):
        link = gateway.enabled(ws)
        return adapters.settings.grants(link.call("grant.list", {"active": query.active}), link.has("read_ext/2"))
