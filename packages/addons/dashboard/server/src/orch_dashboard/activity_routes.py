"""The Activity page's one read route: a closed query over `activity.list`, nothing forwarded as typed."""

from typing import Annotated, Literal

from fastapi import Depends, Query
from pydantic import BaseModel, ConfigDict, Field

from .adapters import activity as adapt
from .reads import Key, workspace

Who = Annotated[str, Field(pattern=r"^(agents|p_[0-9a-f]{32})$")]


class ActivityQuery(BaseModel):
    model_config = ConfigDict(extra="forbid")
    group: Literal["all", "tickets", "work", "talk", "reviews", "files", "workspace"] = "all"
    who: Who | None = None
    ticket: Key | None = None
    limit: int = Field(default=50, ge=1, le=100)
    page: str | None = Field(default=None, min_length=1, max_length=512, pattern=r"^[A-Za-z0-9_.=-]+$")


def host_args(query: ActivityQuery) -> dict:
    args: dict = {"limit": query.limit, "order": "desc"}
    if query.group == "workspace":
        args["scope"] = "workspace"
    elif query.group != "all":
        args["types"] = adapt.GROUP_TYPES[query.group]
    if query.ticket:
        args["ticket"] = query.ticket
        if query.group == "workspace":
            args["scope"] = "tickets"  # a ticket has no workspace-log events
    if query.who == "agents":
        args["actor_kind"] = ["agent"]
    elif query.who:
        args["person"] = query.who
    if query.page:
        args["page"] = query.page
    return args


def register(app, gateway):
    @app.get("/api/w/{ws}/activity")
    def activity(query: Annotated[ActivityQuery, Query()], ws: str = Depends(workspace)):
        link = gateway.enabled(ws)
        if not link.supports("activity.list", "activity/1"):
            return {"available": False, "items": [], "next_page": None}
        return adapt.feed(link.call("activity.list", host_args(query)), link.call("member.list"))
