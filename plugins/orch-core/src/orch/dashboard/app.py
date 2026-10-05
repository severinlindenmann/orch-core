from __future__ import annotations

import asyncio
import contextlib
import logging
import re
from pathlib import Path

from urllib.parse import quote, urlsplit

from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi.responses import PlainTextResponse
from starlette.middleware.gzip import GZipMiddleware

from orch.dashboard import factory_runner
from orch.dashboard.addon_files import DOWNLOAD_TTL, MAX_UPLOAD, OneTimeStore, sweep_addon_io
from orch.dashboard.assets import AssetFiles
from orch.dashboard.auth import auth_middleware

log = logging.getLogger("orch.dashboard")

STATIC_DIR = Path(__file__).with_name("static")
# Never compressed: the live stream (it must flush each frame), artifacts and addon downloads (served as stored,
# with their own CSP; a PDF viewer may ask for byte ranges).
_NO_GZIP = re.compile(r"^/(?:events|a/|addons/[a-z][a-z0-9-]*/files/)")
PAGE_CSP = ("default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
            "frame-src 'self'; frame-ancestors 'none'; form-action 'self'; base-uri 'none'")


_ADDON_FILE = re.compile(r"^/addons/[a-z][a-z0-9-]*/files/[A-Za-z0-9_-]+$")
_ADDON_ACTION = re.compile(r"^/addons/(?P<name>[a-z][a-z0-9-]*)/actions/(?P<action>[a-z][a-z0-9_]*)$")
_MULTIPART_SLACK = 65536  # form fields and part headers around the file itself
SMALL_ACTION_BODY = 65536  # the whole body of an addon action POST that takes no file (64 KiB)
STORE_SWEEP_SECONDS = 30.0  # downloads/reveals expire lazily on their own next put/pop; this clears unclaimed ones


class _UploadTooLarge(Exception):
    """Raised from inside the wrapped ASGI receive() once the actual bytes delivered cross the cap; caught
    around call_next so the downstream app (still mid-parse) never gets to finish or respond itself."""


def _action_cap(request) -> tuple[int, bool]:
    """(body cap, takes a file) for the addon action this POST targets. An action whose manifest has
    accepts_file gets its own max_bytes (never above the hard cap) plus room for the multipart framing; every
    other action, an unknown one included, gets SMALL_ACTION_BODY for the whole body. Never trust the URL alone."""
    m = _ADDON_ACTION.match(request.url.path)
    if m:
        la = request.app.state.addons.registry.get(m.group("name"))
        spec = la.manifest.action(m.group("action")) if la else None
        if spec is not None and spec.accepts_file:
            return min(int(spec.accepts_file[0]), MAX_UPLOAD) + _MULTIPART_SLACK, True
    return SMALL_ACTION_BODY, False


async def csp_middleware(request, call_next):
    """Give every dashboard response the core CSP, overwriting any other (whatever its content type);
    only artifacts (/a/), widget frames (/w/, and /wp/ for a wiki page's), a wiki page's files (/wpf/) and addon downloads keep the stricter one
    their route sets."""
    response = await call_next(request)
    if _ADDON_FILE.match(request.url.path) or request.url.path.startswith(("/w/", "/wp/", "/wpf/")):  # the frame's own
        response.headers.setdefault("Content-Security-Policy", "sandbox")  # also the 401 page there
    elif not request.url.path.startswith("/a/"):
        response.headers["Content-Security-Policy"] = PAGE_CSP
    return response


async def upload_limit_middleware(request, call_next):
    """Multipart addon actions need a Content-Length within the action's own cap before any byte is parsed, and
    the bytes actually delivered to any addon action (a file one or not) are counted as they arrive and capped
    the same way — so a lying Content-Length (for instance alongside a Transfer-Encoding header, or any other
    framing mismatch) can never let an unbounded body reach Starlette's form parser, whatever a header claimed."""
    if request.method == "POST" and _ADDON_ACTION.match(request.url.path):
        cap, takes_file = _action_cap(request)
        length = request.headers.get("content-length")
        if takes_file and request.headers.get("content-type", "").lower().startswith("multipart/"):
            if "transfer-encoding" in request.headers:
                return PlainTextResponse("a file upload needs a Content-Length, not a chunked body", status_code=411)
            if length is None or not length.isdigit():
                return PlainTextResponse("a file upload needs a Content-Length", status_code=411)
        if length is not None and length.isdigit() and int(length) > cap:
            return PlainTextResponse("upload too large" if takes_file else "request body too large", status_code=413)
        total = 0
        aborted = False
        # This relies on a Starlette internal: `request._receive` is the private attribute behind `request.receive`,
        # and BaseHTTPMiddleware's `call_next` hands the downstream app a receive that reads through it (checked on
        # Starlette 1.7). There is no public hook for this. If an upgrade changes it, the swap below silently stops
        # counting; test_body_bytes_past_the_actions_cap_are_refused_whatever_content_length_claims
        # (tests/test_addon_files.py) sends a lying Content-Length at the ASGI level and fails if that happens, so
        # never skip it on a Starlette or FastAPI upgrade.
        original_receive = request._receive  # the request's own receive, swapped before any multipart parsing

        async def capped_receive():
            nonlocal total, aborted
            message = await original_receive()
            if message["type"] == "http.request":
                total += len(message.get("body") or b"")
                if total > cap:
                    aborted = True
                    raise _UploadTooLarge()
            return message

        request._receive = capped_receive
        try:
            response = await call_next(request)
        except _UploadTooLarge:
            response = None
        # The route's own body parsing (FastAPI) may catch _UploadTooLarge itself and turn it into some other
        # response; `aborted` is set synchronously before it is ever raised, so it is trusted either way.
        if aborted:
            return PlainTextResponse("upload too large" if takes_file else "request body too large", status_code=413)
        return response
    return await call_next(request)


class CompressMiddleware:
    """gzip (level 6: most of level 9's gain for a fraction of its time) for pages, assets and JSON of 1 KiB or more,
    when the browser accepts it; Today at 300 tickets goes from 1.3 MB to about 40 KB."""

    def __init__(self, app):
        self.app = app
        self.gzip = GZipMiddleware(app, minimum_size=1024, compresslevel=6)

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and not _NO_GZIP.match(scope["path"]):
            await self.gzip(scope, receive, send)
        else:
            await self.app(scope, receive, send)


class RequestScopeMiddleware:
    """Every dashboard GET runs in one `store.request_scope()`: the page, its addon widgets and the menu badges see
    one ticket scan, one stat per file and one link index instead of each reading the disk again. Not for the
    live-update stream (it outlives any one read) or static files."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        from orch.core import store

        if (scope["type"] == "http" and scope["method"] in ("GET", "HEAD")
                and not scope["path"].startswith(("/static/", "/events"))):
            with store.request_scope():
                await self.app(scope, receive, send)
        else:
            await self.app(scope, receive, send)


async def outbox_loop(ws, seconds: float, pumper) -> None:
    """Hand new events to addons with the events capability and drain their outboxes (v2 §11.8). Each addon
    runs in its own thread with a timeout (OutboxPump), so a hanging one never holds up the others."""
    from orch.addons.loader import _log_error

    while True:
        await asyncio.sleep(seconds)
        try:
            await pumper.run_round(ws.addons)
        except Exception:  # never let one bad round stop the loop
            _log_error(ws, "dashboard", "outbox loop")


def warm_up(ws) -> None:
    """Compile every page template and fill the parsed-ticket cache, so the first page after `orch serve` does not
    pay for either (about 115 ms of Jinja compilation and every open ticket's parse)."""
    from orch.core import query, store
    from orch.dashboard.views import TEMPLATES

    try:
        for name in TEMPLATES.env.list_templates(extensions=["html"]):
            TEMPLATES.env.get_template(name)
        for entry in store.scan(ws):
            if entry.status != "done" and entry.meta is not None:
                try:
                    store.read_ticket(entry.path)
                except Exception:  # noqa: BLE001 - a broken file shows on its page, not here
                    pass
        query.needs_you(ws)
    except Exception:  # noqa: BLE001 - only a head start; pages do the same work themselves
        log.debug("warm-up failed", exc_info=True)


async def needs_loop(ws, seconds: float) -> None:
    """Keep this workspace's needs-count file current for the other workspaces' switchers,
    also while no tab is open here."""
    from orch.dashboard import switcher

    while True:
        await asyncio.sleep(seconds)
        await asyncio.to_thread(switcher.refresh_needs, ws)


async def store_sweep_loop(seconds: float, *stores) -> None:
    """Clear downloads and reveals past their TTL even when nobody ever asks for them again (put/pop only
    expire lazily, on their own next call)."""
    while True:
        await asyncio.sleep(seconds)
        for store in stores:
            store.sweep()


async def form_error(request, exc):
    """A missing or malformed form field goes back to the page with a flash, never raw 422 JSON."""
    from orch.dashboard.views import back

    target = "/"
    referer = urlsplit(request.headers.get("referer", ""))
    same_origin = referer.netloc and referer.netloc == request.headers.get("host", "")
    if same_origin and referer.path.startswith("/") and not referer.path.startswith("//"):
        target = referer.path
    elif "ref" in request.path_params:
        target = "/t/" + quote(str(request.path_params["ref"]), safe="")
    return back(target, err="some form fields were missing or invalid")


def create_app(ws, token: str, *, port: int | None = None) -> FastAPI:
    from orch.dashboard import (routes_actions, routes_activity, routes_addons, routes_agent_start, routes_board,
                                routes_design, routes_guide, routes_live, routes_new, routes_permits, routes_reports, routes_terminals, routes_theme,
                                routes_ticket, routes_widgets, routes_workspace, setup_state, switcher)
    from orch.addons.outbox import OutboxPump
    from orch.addons.runtime import AddonRuntime
    from orch.addons.scheduler import Scheduler

    if port is not None:
        # The switcher is a convenience: a read-only or full config directory must never stop
        # `orch serve` from starting, it only leaves this workspace out of the others' lists.
        try:
            switcher.register(ws, port)
        except OSError as exc:
            log.warning("could not record this workspace in workspaces.json for the switcher: %s", exc)

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI):
        seconds = float(ws.config["dashboard"].get("pull_seconds", 60))
        await asyncio.to_thread(switcher.refresh_needs, ws)  # current from the start, before any page
        await asyncio.to_thread(app.state.addons.reload)  # import the enabled, trusted addons once, before any page
        tasks = [asyncio.create_task(needs_loop(ws, seconds))]
        # Setup checks (git calls) every minute in a thread, the first round right away; then the first page's
        # other cold costs (template compilation, a first parse of every open ticket). Neither delays readiness.
        tasks.append(asyncio.create_task(setup_state.state(ws).run_forever()))
        tasks.append(asyncio.create_task(asyncio.to_thread(warm_up, ws)))
        # Both do nothing while no addon is enabled; one enabled later starts without a restart.
        tasks.append(asyncio.create_task(app.state.scheduler.run_forever()))
        tasks.append(asyncio.create_task(outbox_loop(ws, seconds, app.state.outbox)))
        tasks.append(asyncio.create_task(store_sweep_loop(STORE_SWEEP_SECONDS, app.state.downloads, app.state.reveals)))
        tasks.append(asyncio.create_task(factory_runner.loop(ws)))  # AI Factory: idle unless the human started an epic
        try:
            yield
        finally:
            for task in tasks:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
            await app.state.scheduler.shutdown()  # fetches and outbox rounds still in flight
            await app.state.outbox.shutdown()

    app = FastAPI(title="orch", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
    app.exception_handler(RequestValidationError)(form_error)
    app.state.ws = ws
    app.state.token = token
    app.state.scheduler = Scheduler(ws, live=lambda: routes_live.subscriber_count(ws) > 0)
    app.state.outbox = OutboxPump(ws)
    app.state.addons = AddonRuntime(ws)  # what pages ask of addons: cache reads and widgets, never ctx.run
    app.state.downloads = OneTimeStore(DOWNLOAD_TTL)  # token -> a FileResult staged in out/, served once
    app.state.reveals = OneTimeStore(DOWNLOAD_TTL)  # token -> a Reveal, shown once
    try:
        sweep_addon_io(ws)  # uploads and downloads left over from a crash
    except OSError as exc:
        log.warning("could not clear old addon uploads and downloads: %s", exc)
    app.middleware("http")(upload_limit_middleware)  # innermost: only an authenticated request is size-checked
    app.middleware("http")(auth_middleware)
    app.middleware("http")(csp_middleware)  # added after these = outside them, so it also covers the 401 page
    app.add_middleware(RequestScopeMiddleware)
    app.add_middleware(CompressMiddleware)  # outermost: compresses whatever the stack produced
    app.mount("/static", AssetFiles(directory=str(STATIC_DIR)), name="static")
    for module in (routes_board, routes_ticket, routes_actions, routes_new, routes_workspace, routes_live, routes_theme,
                   routes_activity, routes_permits, routes_reports, routes_agent_start, routes_addons, routes_design, routes_guide, routes_terminals,
                   routes_widgets):
        app.include_router(module.router)
    return app
