"""Worktrees (H11d, host contract section 12.7): one git worktree per ticket and linked repo.

Typed routes with closed bodies, like the agents: the host op and its arguments are fixed here. `worktree.list` is a
read (with `check`: what the host's reader sees in each folder now). `worktree.create` and `worktree.remove` are
dashboard-direct writes (Tier A, no signature): the host decides who may (its column rules, the `worktrees` add-on
grant, the runner gate, a rate limit) and the browser draws one idem per click and keeps it for every retry. The
force-remove stays a person's terminal command; the page never offers it. Host text is display-only: hidden characters
are withheld, never repaired.
"""

from __future__ import annotations

from typing import Annotated, Literal

from fastapi import Depends, FastAPI, Query
from pydantic import BaseModel, ConfigDict, StringConstraints

from . import reads
from .adapters import board_statuses
from .adapters.addons import display
from .errors import HostError
from .gateway import Gateway
from .terminals import Idem

Repo = Annotated[str, StringConstraints(pattern=r"^[a-z0-9][a-z0-9._-]{0,63}$")]
#: the closed set of `Worktree.state` (section 12.7); anything else reads `unknown`
STATES = ("ok", "unpinned", "stale_branch", "missing", "moved")


class ListQuery(BaseModel):
    model_config = ConfigDict(extra="forbid")
    ticket: reads.Key | None = None


class CreateBody(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    ticket: reads.Key
    repo: Repo | None = None
    base: Literal["default", "current"] = "default"
    idem: Idem

    def host_args(self) -> dict:
        args = {"ticket": self.ticket, "base": self.base}
        if self.repo:
            args["repo"] = self.repo
        return args


class RemoveBody(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    ticket: reads.Key
    repo: Repo | None = None
    idem: Idem

    def host_args(self) -> dict:
        args = {"ticket": self.ticket}
        if self.repo:
            args["repo"] = self.repo
        return args


def row(data, me: str | None, titles: dict | None = None) -> dict:
    """One `Worktree` (section 12.7) for the page. `dirty` null is unknown (not checked, or unreadable), never zero."""
    by = data.get("created_by") if isinstance(data.get("created_by"), dict) else {}
    dirty = data.get("dirty")
    state = data.get("state")
    key = data.get("ticket")
    return {
        "ticket": display(key),
        "title": display((titles or {}).get(key)),
        "repo": display(data.get("repo")),
        "branch": display(data.get("branch")),
        "path": display(data.get("path")),
        "state": state if state in STATES else "unknown",
        "dirty": dirty if type(dirty) is int and dirty >= 0 else None,
        "checked": isinstance(data.get("checked_at"), str),
        "created_at": display(data.get("created_at")),
        "by": by.get("kind") if by.get("kind") in ("person", "agent") else None,
        "mine": by.get("kind") == "person" and by.get("id") == me,
    }


def register(app: FastAPI, gateway: Gateway) -> None:
    def titles(ws, wanted):
        """Ticket titles for the rows (best effort: a title the board cannot give is left out, the key still shows)."""
        if not wanted:
            return {}
        try:
            rows, _ = reads.all_rows(gateway, ws, {"status": board_statuses(), "sort": "updated"}, cap=400)
        except HostError:
            return {}
        return {r["key"]: r["title"] for r in rows if r.get("key") in wanted and isinstance(r.get("title"), str)}

    @app.get("/api/w/{ws}/worktrees")
    def listing(query: Annotated[ListQuery, Query()], ws: str = Depends(reads.workspace)):
        link = gateway.enabled(ws)
        if not link.supports("worktree.list", "worktree/1"):
            return {"available": False, "rows": []}
        args = {"check": True, **({"ticket": query.ticket} if query.ticket else {})}
        data = link.call("worktree.list", args)
        me = link.welcome["peer"]["person"]
        rows = data.get("worktrees") if isinstance(data, dict) else None
        rows = [r for r in rows if isinstance(r, dict)] if isinstance(rows, list) else []
        names = titles(ws, {r.get("ticket") for r in rows}) if not query.ticket else {}
        return {"available": True, "rows": [row(r, me, names) for r in rows]}

    @app.post("/api/w/{ws}/worktrees/create")
    def create(body: CreateBody, ws: str = Depends(reads.workspace)):
        link = gateway.enabled(ws)
        data = link.call("worktree.create", body.host_args(), write=True, idem=body.idem)
        made = data.get("worktree") if isinstance(data, dict) else None
        return {"worktree": row(made, link.welcome["peer"]["person"]) if isinstance(made, dict) else None}

    @app.post("/api/w/{ws}/worktrees/remove")
    def remove(body: RemoveBody, ws: str = Depends(reads.workspace)):
        data = gateway.enabled(ws).call("worktree.remove", body.host_args(), write=True, idem=body.idem)
        return {"removed": isinstance(data, dict) and data.get("removed") is True}
