"""Closed M6 read routes and the two fixed records child actions."""

from typing import Annotated, Literal

from fastapi import Depends, Query, Request
from pydantic import BaseModel, ConfigDict, Field

from .adapters import addons as adapt
from .adapters import host_status
from .errors import fail
from .reads import ContentQuery, UiStatus, workspace
from .records_child import RecordsChild


class QuickQuery(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: list[UiStatus | Literal["waiting"]] | None = Field(default=None, min_length=1, max_length=8)
    max_commits: int | None = Field(default=None, ge=1, le=100)
    max_files: int | None = Field(default=None, ge=1, le=1000)
    limit: int = Field(default=100, ge=1, le=200)
    page: str | None = Field(default=None, min_length=1, max_length=512)


class Commit(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    push: bool


def register(app, gateway):
    child = RecordsChild()
    app.state.records_child = child

    @app.get("/api/w/{ws}/quick")
    def quick(query: Annotated[QuickQuery, Query()], ws: str = Depends(workspace)):
        link = gateway.enabled(ws)
        if not link.supports("quick.list", "quick/1"):
            return {"available": False, "rows": None, "next_page": None, "total": None}
        args = query.model_dump(exclude_none=True)
        if query.status is not None:
            args["status"] = [host_status(s) for s in query.status]
        return adapt.quick(link.call("quick.list", args))

    def records_link(ws, *, write=False, push=False, auto_off=False):
        link = gateway.enabled(ws)
        ops = ["records.status"]
        if auto_off:
            ops += ["records.note"]
        if write:
            ops += ["records.note", "records.snapshot"]
            if push:
                ops += ["records.export_check"]
        if not all(link.supports(op, "records/1") for op in ops):
            return None
        return link

    @app.get("/api/w/{ws}/records")
    def records(query: Annotated[ContentQuery, Query()], ws: str = Depends(workspace)):
        link = records_link(ws)
        if link is None:
            return {"available": False}
        # The host is the authority: its owner/maintainer refusal is returned unchanged.
        return adapt.records(link.call("records.status", {}))

    @app.get("/api/w/{ws}/records/current")
    def current(query: Annotated[ContentQuery, Query()], ws: str = Depends(workspace)):
        link = records_link(ws)
        if link is None:
            raise fail("op.unsupported", exit=1)
        link.call("records.status", {})
        return child.run(ws, status=True, state=gateway.state, run_dir=gateway.run)

    def run(ws, *, push=False, auto_off=False):
        link = records_link(ws, write=not auto_off, push=push, auto_off=auto_off)
        if link is None:
            raise fail("op.unsupported", exit=1)
        link.call("records.status", {})  # authorize this person before any local process is started
        return child.run(ws, push=push, auto_off=auto_off, state=gateway.state, run_dir=gateway.run)

    @app.post("/api/w/{ws}/records/commit")
    def commit(body: Commit, ws: str = Depends(workspace)):
        return run(ws, push=body.push)

    @app.post("/api/w/{ws}/records/auto-off")
    async def auto_off(request: Request, ws: str = Depends(workspace)):
        if await request.body():
            raise fail("invalid.input", exit=5)
        from starlette.concurrency import run_in_threadpool

        return await run_in_threadpool(run, ws, auto_off=True)
