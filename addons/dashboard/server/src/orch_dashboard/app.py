import asyncio
import re
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated

from fastapi import Depends, FastAPI, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
from pydantic import BaseModel, ConfigDict, Field

from . import adapters, reads
from .auth import Auth
from .errors import HostError, fail, host_response, response
from .gateway import Gateway
from .live import Live, SessionStream
from .security import Security


class LaunchCode(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    code: str = Field(min_length=43, max_length=43, pattern=r"^[A-Za-z0-9_-]+$")


# The browser app's own addresses (permanent URLs, G2): served index.html so a reload or pasted link opens the page.
SPA_PATH = re.compile(
    r"w/[A-Za-z0-9_-]+(?:/(?:today|board|tickets|identity)|/ticket/[A-Za-z][A-Za-z0-9]{0,15}-[0-9]{1,9})?"
    r"|ticket/[A-Za-z][A-Za-z0-9]{0,15}-[0-9]{1,9}"
)


def create_app(port: int, gateway=None, auth=None, static=None):
    gateway, auth = gateway or Gateway(), auth or Auth()
    static = Path(static) if static else Path(__file__).parent / "static"

    live = Live(gateway, auth)

    @asynccontextmanager
    async def lifespan(app):
        yield
        await live.close()
        gateway.close()

    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
    app.state.auth = auth
    app.state.live = live
    app.add_middleware(Security, port=port, auth=auth)

    @app.exception_handler(HostError)
    async def host_error(request, exc):
        return host_response(exc)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request, exc):
        return response("invalid.input", 400)  # never echo input or credentials

    @app.get("/auth/probe")
    def probe(n: Annotated[str, Query(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_-]+$")]):
        return PlainTextResponse(auth.probe(n))

    @app.post("/auth/session")
    def session(body: LaunchCode):
        value, status = auth.exchange(body.code)
        return (
            JSONResponse(value) if value else response("rate.limited" if status == 429 else "session.required", status)
        )

    @app.post("/auth/logout")
    def logout(request: Request):
        auth.logout(request.state.token)
        return {"ok": True}

    @app.get("/api/events")
    async def events(request: Request, ws: str = Depends(reads.workspace)):
        await asyncio.to_thread(gateway.enabled, ws)
        last = request.headers.get("last-event-id")
        if last and len(last) > 256:
            raise fail("invalid.input", exit=5)
        w, tab = live.attach(ws, request.state.token, last)
        return SessionStream(live.stream(w, tab), auth, request.state.token)

    @app.get("/api/w/{ws}/head")
    def head(ws: str = Depends(reads.workspace)):
        return {"sub_cursor": gateway.call(ws, "ticket.query", {"limit": 1})["sub_cursor"]}

    @app.get("/api/session")
    def session_info():
        workspaces = gateway.workspaces()
        return {
            "features": sorted({f for ws in workspaces for f in ws.features}),
            "workspaces": {ws.id: ws.features for ws in workspaces},
        }

    @app.get("/api/workspaces")
    def workspaces():
        return gateway.workspaces()

    @app.get("/api/me")
    def me():
        return [
            adapters.me(gateway.call(ws.id, "whoami"), gateway.call(ws.id, "member.list"), ws.id)
            for ws in gateway.workspaces()
            if "identity" in ws.features
        ]

    @app.get("/api/w/{ws}/identity")
    def identity(ws: str = Depends(reads.workspace)):
        show, status = gateway.call(ws, "workspace.show"), gateway.call(ws, "host.status")
        who = adapters.me(gateway.call(ws, "whoami"), gateway.call(ws, "member.list"), ws)
        return {
            "uuid": show["id"],
            "prefix": show["prefix"],
            "genesis": show.get("genesis"),
            "epoch": show.get("epoch"),
            "key_fingerprint": show.get("wsk_fingerprint"),
            "created_at": None,
            "tier": status["tier"],
            "me": who,
        }

    reads.register(app, gateway)

    @app.get("/{path:path}")
    def spa(path: str):
        # Closed SPA surface. Unknown APIs/auth paths can never fall through to index.html.
        if path.startswith("assets/"):
            candidate = (static / path).resolve()
            if candidate.is_relative_to((static / "assets").resolve()) and candidate.is_file():
                return FileResponse(candidate, headers={"Cache-Control": "public, max-age=31536000, immutable"})
        elif path in ("", "favicon.svg") or SPA_PATH.fullmatch(path):
            candidate = static / ("favicon.svg" if path == "favicon.svg" else "index.html")
            if candidate.is_file():
                return FileResponse(candidate, headers={"Cache-Control": "no-store"})
        return response("not_found", 404)

    return app
