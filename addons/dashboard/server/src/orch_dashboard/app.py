import asyncio
import re
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated

from fastapi import Depends, FastAPI, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse
from pydantic import BaseModel, ConfigDict, Field

from . import (
    activity_routes,
    adapters,
    addon_routes,
    addon_switches,
    agents,
    click,
    handoff,
    hub,
    hubapprove,
    phone,
    reads,
    riskack,
    settings,
    shares,
    signprefs,
    terminals,
    testrun,
    usage,
    widget_frame,
    workdirs,
    worktrees,
)
from .adapters.setup import SetupProxy
from .adapters.setup import register as register_setup
from .auth import Auth
from .errors import HostError, fail, host_response, response
from .explorer import routes as explorer_routes
from .gateway import Gateway, state_dir
from .live import Live, SessionStream
from .security import Security
from .wiki import routes as wiki_routes


class LaunchCode(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    code: str = Field(min_length=43, max_length=43, pattern=r"^[A-Za-z0-9_-]+$")


# The browser app's own addresses (permanent URLs, G2): served index.html so a reload or pasted link opens the page.
# ONE closed list, in the router's own spelling (src/app/router.tsx, `$x` = a parameter). A test walks every route of
# the router and every real-host settings tab and fails when one is missing here, so a new page cannot forget it.
SETTINGS_TABS = (
    "general",
    "members",
    "gates",
    "grants",
    "addons",
    "records",
    "devices",
    "hub",
)  # HOST_TABS (settings/tabs.ts)
SPA_ROUTES = (
    "",  # Today
    "today",  # older spelling of Today
    "board",
    "tickets",
    "tickets/new",
    "ticket/$key",
    "artifacts",
    "agents",
    "quick",
    "drop",
    "checklist",
    "feedback",
    "worktrees",
    "terminals",
    "explorer",
    "shares",
    "activity",
    "usage",
    "guide",  # ?page=<x> is a query: not part of the path
    "settings",
    "settings/$tab",
    "settings/addon/$name",
    "settings/addons/$name",
    "addon/$name/$page",
)
# Routes that also open without a workspace in the address (the ticket key names its own workspace) ...
SPA_BARE = ("ticket/$key", "checklist", "feedback")
# ... and the ones that open ONLY without one (urls.ts isWorkspacePath: the checklist is not a workspace page).
SPA_BARE_ONLY = ("checklist", "feedback")
_PARAM = {
    "key": r"[A-Za-z][A-Za-z0-9]{0,15}-[0-9]{1,9}",
    "tab": "(?:" + "|".join(SETTINGS_TABS) + ")",
    "name": r"[A-Za-z0-9_-]{1,64}",
    "page": r"[A-Za-z0-9_-]{1,64}",
}


def _spa_pattern(route: str) -> str:
    return "/".join(_PARAM[seg[1:]] if seg.startswith("$") else re.escape(seg) for seg in route.split("/") if seg)


SPA_PATH = re.compile(
    "|".join(
        [
            r"w/[A-Za-z0-9_-]+(?:/(?:"
            + "|".join(_spa_pattern(r) for r in SPA_ROUTES if r and r not in SPA_BARE_ONLY)
            + "))?"
        ]
        + [_spa_pattern(r) for r in SPA_BARE]
    )
)


# Every root-level file index.html references (public/ of the UI). A test checks the built index.html against this list.
ROOT_FILES = frozenset({"favicon.svg", "loading.css"})


def create_app(port: int, gateway=None, auth=None, static=None, setup_proxy=None):
    gateway, auth = gateway or Gateway(), auth or Auth()
    static = Path(static) if static else Path(__file__).parent / "static"

    live = Live(gateway, auth)
    tickets = terminals.Tickets()
    streams = terminals.Streams(live)
    live.terminals = lambda token: streams.open.get(token, 0)

    @asynccontextmanager
    async def lifespan(app):
        yield
        await live.close()
        gateway.close()

    # FastAPI >= 0.140 ships OpenTelemetry on by default: off here, whatever the environment says (no request paths or
    # exception text may ever leave this local server).
    telemetry = {"tracing": False, "metrics": False, "logs": False, "operation_spans": False, "auto_configure": False}
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan, telemetry=telemetry)
    app.state.auth = auth
    app.state.live = live
    app.state.term_tickets = tickets
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
        try:
            app.state.cancel_session(request.state.token)  # an open sign request is cancelled with its session
            app.state.cancel_clicks(request.state.token)
            tickets.forget(request.state.token)  # its unused browser tickets go with it
        finally:
            auth.logout(request.state.token)  # the session ends even if a cancel failed
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
    def session_info(request: Request):
        workspaces = gateway.workspaces()
        return {
            # How this browser session was opened: "local" (a launch code from this computer; the page hides Log out).
            "login": auth.login_kind(request.state.token) or "unknown",
            "features": sorted({f for ws in workspaces for f in ws.features}),
            "workspaces": {ws.id: ws.features for ws in workspaces},
        }

    @app.get("/api/workspaces")
    def workspaces():
        return gateway.workspaces()

    @app.get("/api/host")
    def host():
        return gateway.host_info()

    def identity_row(ws):
        link = gateway.enabled(ws)
        return adapters.me(
            gateway.call(ws, "whoami"),
            gateway.call(ws, "member.list"),
            ws,
            runner_supported=link.supports("whoami", "ops/1") and link.has("run/1"),
        )

    @app.get("/api/me")
    def me():
        return [identity_row(ws.id) for ws in gateway.workspaces() if "identity" in ws.features]

    @app.get("/api/w/{ws}/identity")
    def identity(ws: str = Depends(reads.workspace)):
        show, status = gateway.call(ws, "workspace.show"), gateway.call(ws, "host.status")
        who = identity_row(ws)
        return {
            "uuid": show["id"],
            "prefix": show["prefix"],
            "genesis": show.get("genesis"),
            "epoch": show.get("epoch"),
            "key_fingerprint": show.get("wsk_fingerprint"),
            "created_at": None,
            "tier": status["tier"],
            "me": who,
            "runner": who.runner,
        }

    register_setup(app, auth, setup_proxy or SetupProxy(state_dir() / "setup" / "setup.sock"), port)
    reads.register(app, gateway)
    handoff.register(app, gateway)
    click.register(app, gateway)
    hubapprove.register(app, gateway)
    hub.register(app, gateway)
    settings.register(app, gateway)
    addon_switches.register(app, gateway)
    signprefs.register(app, gateway)
    riskack.register(app, gateway)
    phone.register(app, gateway)
    addon_routes.register(app, gateway)
    activity_routes.register(app, gateway)
    terminals.register(app, gateway, tickets)
    agents.register(app, gateway)
    testrun.register(app, gateway)
    worktrees.register(app, gateway)
    workdirs.register(app, gateway)
    usage.register(app, gateway)
    shares.register(app, gateway)
    explorer_routes.register(app, gateway)
    wiki_routes.register(app, gateway)
    widget_frame.register(app, gateway)
    terminals.register_socket(app, gateway, auth, tickets, streams)

    @app.get("/{path:path}")
    def spa(path: str, request: Request):
        # Closed SPA surface. Unknown APIs/auth paths can never fall through to index.html.
        if path.startswith("assets/"):
            candidate = (static / path).resolve()
            if candidate.is_relative_to((static / "assets").resolve()) and candidate.is_file():
                return FileResponse(candidate, headers={"Cache-Control": "public, max-age=31536000, immutable"})
        elif path in ROOT_FILES:
            # Files index.html names at the root (favicon, boot-shell css): a fixed list, never a path from the request.
            candidate = static / path
            if candidate.is_file():
                return FileResponse(candidate, headers={"Cache-Control": "no-store"})
        elif path in ("", "setup") or SPA_PATH.fullmatch(path):
            # The file:// launcher is cross-site too: serve its shell, but forbid
            # automatic session sharing in foreign documents. Missing metadata keeps
            # old-browser compatibility (and cannot protect their foreign loads).
            sites = request.headers.getlist("sec-fetch-site")
            foreign = bool(sites) and sites not in (["none"], ["same-origin"])
            candidate = static / "index.html"
            if candidate.is_file():
                if foreign:
                    html = candidate.read_text(encoding="utf-8")
                    marked = html.replace("<head>", '<head><meta name="orch-nav" content="foreign">', 1)
                    if marked == html:
                        # Fail closed (Opus #444 re-check R-1): without the marker a foreign tab would join sessions.
                        return response("internal", 500)
                    return HTMLResponse(marked, headers={"Cache-Control": "no-store"})
                return FileResponse(candidate, headers={"Cache-Control": "no-store"})
        return response("not_found", 404)

    return app
