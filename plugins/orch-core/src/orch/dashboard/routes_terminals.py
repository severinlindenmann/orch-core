"""Terminals (issue #40): every agent session Mission Control started in orch's tmux server, live, and one of them
full size to watch or type into, with what the agent is doing (agentinfo).

Screens reach the browser as server-rendered, escaped HTML: in the page itself (works without JS) and then over
server-sent events. Keys come back as a same-origin JSON POST. Every route sits behind the token cookie
(auth_middleware) and answers only to a request from this machine with a loopback Host (terminals.local_request: not
over `orch serve --lan`, not after DNS rebinding); every POST also needs a strict same-origin Origin header, and a name
must be one of this workspace's sessions before tmux hears it.
"""
from __future__ import annotations

import asyncio
import json
import re
import threading
import time
from typing import Annotated

from fastapi import APIRouter, Form, Request
from fastapi.responses import PlainTextResponse, Response, StreamingResponse

from orch.core import store
from orch.dashboard import agentinfo, launch, terminals
from orch.dashboard.auth import strict_same_origin
from orch.dashboard.data import agent_start
from orch.dashboard.views import back, confirm_page, error_text, page
from orch.dashboard.reach import remote_origin
from orch.errors import OrchError, ValidationError

router = APIRouter()

TILE_SECONDS = 1.0  # the grid's refresh while something changes
TILE_IDLE_SECONDS = 5.0  # ... backing off (doubling) to this while nothing does
VIEW_SECONDS = 0.2  # the full view's refresh while the screen changes or someone types
VIEW_IDLE_SECONDS = 2.0  # ... backing off to this while it does not; a key or size POST wakes it at once
INFO_SECONDS = 3.0  # how often the agent info is read again (it changes slower than the screen)
HEARTBEAT_SECONDS = 15.0
_WAKE: dict[str, set] = {}  # session name -> the asyncio.Events of its open full views, set by a key or size POST


async def _nap(name: str, seconds: float) -> bool:
    """Sleep up to `seconds`; True when a key or size POST for `name` woke it early."""
    event = asyncio.Event()
    _WAKE.setdefault(name, set()).add(event)
    try:
        await asyncio.wait_for(event.wait(), seconds)
        return True
    except asyncio.TimeoutError:
        return False
    finally:
        waiting = _WAKE.get(name)
        if waiting is not None:
            waiting.discard(event)
            if not waiting:
                _WAKE.pop(name, None)


def _wake(name: str) -> None:
    for event in list(_WAKE.get(name, ())):
        event.set()
MAX_BODY = 65536
NO_SCREEN = {"html": "", "trim": "", "tail": "", "cols": 80, "rows": 24}  # a session that ended between listing and capture
_ORDER = {"waiting": 0, "busy": 1, "idle": 2}


def _off(request: Request) -> bool:
    """Every route but the Terminals page itself answers 404 while the terminals addon is off (or tmux is missing), and
    to any request that is not a local one."""
    return not terminals.enabled(request.app.state.ws, request)


def _sse(event: str, data) -> str:
    return f"event: {event}\ndata: {json.dumps(data)}\n\n"


_TICKET_NAME = re.compile(r"([A-Z][A-Z0-9]*-[0-9]+)(?:-[0-9]+)?")


def _named_ticket(ws, name: str) -> str:
    """The ticket a session is named for (`DEMO-1`, or `DEMO-1-2` for a second one), when it exists: shown as its
    ticket until the agent claims one. "" otherwise (`scratch`)."""
    m = _TICKET_NAME.fullmatch(name)
    if m is None:
        return ""
    try:
        store.load(ws, m.group(1))
    except OrchError:
        return ""
    return m.group(1)


def _info(ws, s, screen=None) -> dict:
    """agentinfo for session `s`, with the model and context read off its screen when its transcript gave none."""
    return agentinfo.with_screen(agentinfo.info(ws, s), screen)


def _live(s, i: dict, ws=None) -> dict:
    """What a tile or the summary line shows, as JSON for the page and the stream. `title` is the session's own name
    (what you started it as); what the agent is working on (its transcript's title) is `topic`."""
    claimed = i.get("tickets") or []
    first = claimed[0] if claimed else None
    named = "" if first or ws is None else _named_ticket(ws, s.name)
    active = i.get("subagents_active") or 0
    return {"status": i.get("status") or "", "now": i.get("now") or "", "kind": i.get("now_kind") or "",
            "title": s.name, "topic": i.get("title") or "", "sig": i.get("sig") or "", "activity": s.activity,
            "ticket": first["id"] if first else named, "tasks": (first or {}).get("tasks") or "",
            "more": len(claimed) - 1 if len(claimed) > 1 else 0,
            "subs": f"{active} subagent{'s' if active != 1 else ''} running" if active else "",
            "model": i.get("model") or "", "context": agentinfo.compact(i.get("context")) if i.get("context") else "",
            "cache": agentinfo.cache_label(i.get("cache")), "cold": bool(i.get("cache") and not i["cache"]["warm"])}


def _rows(ws) -> list[dict]:
    rows = []
    found = terminals.sessions(ws)
    screens = terminals.capture_many([s.name for s in found])  # one tmux call for every screen
    notes = launch.launch_notes(ws)  # "Started on ..." from an addon's launch plan (model routing), when there is one
    for s in found:
        screen = screens.get(s.name) or NO_SCREEN
        i = _info(ws, s, screen)
        rows.append({"s": s, "screen": screen, "info": i, "live": _live(s, i, ws), "started": notes.get(s.name, "")})
    rows.sort(key=lambda r: (_ORDER.get(r["info"].get("status"), 3), -r["s"].activity))  # waiting first
    return rows


@router.get("/terminals")
def grid(request: Request):
    ws = request.app.state.ws
    addon_on, found, local = terminals.addon_on(ws), terminals.available(), terminals.local_request(request)
    on = addon_on and found and local
    rows = _rows(ws) if on else []
    harness = terminals.settings(ws.root)["harness"]
    counts = {k: sum(1 for r in rows if r["info"].get("status") == k) for k in ("waiting", "busy", "idle")}
    return page(request, "terminals.html", 200 if on else 404, nav="terminals", title="Terminals", addon_on=addon_on,
                available=found, local=local, rows=rows, root=str(ws.root), counts=counts, harness_label=agent_start.HARNESS_LABELS.get(harness, harness))


@router.get("/terminals/stream")
async def grid_stream(request: Request):
    if _off(request):
        return PlainTextResponse("not found", status_code=404)
    ws = request.app.state.ws

    async def gen():
        # Each tick: one tmux call lists the sessions, one captures every screen (capture_many). While nothing
        # changes the tick backs off from TILE_SECONDS to TILE_IDLE_SECONDS; a hidden tab closes the stream.
        last: dict[str, dict] = {}
        last_live: dict[str, dict] = {}
        names: list[str] | None = None
        quiet, wait, info_at = 0.0, TILE_SECONDS, None
        loop = asyncio.get_running_loop()
        yield ": connected\n\n"
        while not await request.is_disconnected():
            found = await asyncio.to_thread(terminals.sessions, ws)
            now = sorted(s.name for s in found)
            if names is not None and now != names:
                yield _sse("sessions", now)  # one started or ended: the page reloads to redraw its tiles
            names = now
            screens = await asyncio.to_thread(terminals.capture_many, [s.name for s in found])
            changed = {}
            for name, screen in screens.items():
                if screen is not None and screen != last.get(name):
                    changed[name] = last[name] = screen
            sent = False
            if changed:
                yield _sse("screens", changed)
                sent = True
            if info_at is None or loop.time() - info_at >= INFO_SECONDS:  # the agent info changes slower
                info_at = loop.time()
                lives = {}
                for s in found:
                    live = _live(s, await asyncio.to_thread(_info, ws, s, last.get(s.name)), ws)
                    if live != last_live.get(s.name):
                        lives[s.name] = last_live[s.name] = live
                if lives:
                    yield _sse("infos", lives)
                    sent = True
            wait = TILE_SECONDS if sent else min(TILE_IDLE_SECONDS, wait * 2)
            quiet = 0.0 if sent else quiet + wait
            if quiet >= HEARTBEAT_SECONDS:
                yield ": ping\n\n"
                quiet = 0.0
            await asyncio.sleep(wait)

    return StreamingResponse(gen(), media_type="text/event-stream", headers={"Cache-Control": "no-store"})


def _found(request: Request, name: str):
    """This workspace's session `name`, or None: also None for every name while Terminals is off."""
    if _off(request):
        return None
    try:
        return terminals.find(request.app.state.ws, name)
    except ValidationError:
        return None


@router.get("/terminals/{name}")
def view(request: Request, name: str):
    s = _found(request, name)
    if s is None:
        return page(request, "terminal.html", 404, nav="terminals", title="Terminal", s=None, term_name=name,
                    screen=NO_SCREEN)
    screen = terminals.capture(s.name) or NO_SCREEN
    ws = request.app.state.ws
    i = _info(ws, s, screen)
    return page(request, "terminal.html", nav="terminals", title=s.name, s=s, info=i,
                live=_live(s, i, ws), compact=agentinfo.compact, screen=screen,
                started=launch.launch_notes(ws).get(s.name, ""))


@router.get("/terminals/{name}/snapshot")
async def view_snapshot(request: Request, name: str):
    """The screen and summary once, as JSON: what a device polls when its stream misbehaves."""
    s = _found(request, name)
    if s is None:
        return PlainTextResponse("no such terminal", status_code=404)
    ws = request.app.state.ws
    screen = await asyncio.to_thread(terminals.capture, name)
    if screen is None:
        return PlainTextResponse("no such terminal", status_code=404)
    live = _live(s, await asyncio.to_thread(_info, ws, s, screen), ws)
    return Response(json.dumps({"screen": screen, "info": live}), media_type="application/json",
                    headers={"Cache-Control": "no-store"})


@router.get("/terminals/{name}/stream")
async def view_stream(request: Request, name: str):
    s = _found(request, name)
    if s is None:
        return PlainTextResponse("no such terminal", status_code=404)
    ws = request.app.state.ws

    async def gen():
        # One tmux call per tick. While the screen does not change the tick backs off from VIEW_SECONDS to
        # VIEW_IDLE_SECONDS; a key or size POST for this session (_wake) brings it straight back, so typing stays live.
        last = None
        last_live = None
        quiet, wait, info_at = 0.0, VIEW_SECONDS, None
        loop = asyncio.get_running_loop()
        yield ": connected\n\n"
        while not await request.is_disconnected():
            screen = await asyncio.to_thread(terminals.capture, name)
            if screen is None:
                yield _sse("gone", name)
                return
            sent = False
            if screen != last:
                last = screen
                yield _sse("screen", screen)
                sent = True
            if info_at is None or loop.time() - info_at >= INFO_SECONDS:
                info_at = loop.time()
                live = _live(s, await asyncio.to_thread(_info, ws, s, last), ws)
                if live != last_live:
                    last_live = live
                    yield _sse("info", live)
                    sent = True
            wait = VIEW_SECONDS if sent else min(VIEW_IDLE_SECONDS, wait * 2)
            quiet = 0.0 if sent else quiet + wait
            if quiet >= HEARTBEAT_SECONDS:
                yield ": ping\n\n"
                quiet = 0.0
            if await _nap(name, wait):
                wait = VIEW_SECONDS  # someone typed or resized: follow closely again

    return StreamingResponse(gen(), media_type="text/event-stream", headers={"Cache-Control": "no-store"})


async def _json(request: Request):
    body = await request.body()
    if len(body) > MAX_BODY:
        raise ValidationError("body too large")
    try:
        return json.loads(body or b"{}")
    except ValueError as e:
        raise ValidationError("body is not JSON") from e


# Key posts from a paired device. The bridge carries about 1 request a second per device, so a device batches its
# keystrokes (the page sends one post per second) and numbers the posts: `n` must rise with every post of a page
# (`page` names it, so two tabs of one device count apart), so one that arrives twice or late never types twice. Local requests are unchanged.
REMOTE_POSTS, REMOTE_WINDOW_MS = 11, 10_000  # posts per device per window: 1.1 a second, as the page's 1 a second plus a retry
REMOTE_MAX_ITEMS, REMOTE_MAX_CHARS = 64, 2048  # one device post: items, and characters of text
_PAGE = re.compile(r"[A-Za-z0-9_-]{4,64}")
_DEVICE_LOCK = threading.Lock()
MAX_PAGES, MAX_PAGES_PER_DEVICE = 256, 8  # page counters kept (all, and one device's); the oldest go first
_DEVICE_LAST: dict[tuple, int] = {}  # (device, page) -> the highest post number taken
_DEVICE_RATE: dict = {}  # device -> its SlidingLimit


def _device_keys_refusal(device: str, data) -> tuple[int, str] | None:
    """(status, text) when a device's key post must not run; otherwise its post number is taken and None returned."""
    seq = data.get("seq") if isinstance(data, dict) else None
    n = data.get("n") if isinstance(data, dict) else None
    page = data.get("page") if isinstance(data, dict) else None
    if type(n) is not int or n < 1 or not isinstance(page, str) or not _PAGE.fullmatch(page):
        return 400, "key posts from a device carry a post number n and a page name"
    if not isinstance(seq, list) or len(seq) > REMOTE_MAX_ITEMS or sum(
            len(i["text"]) for i in seq if isinstance(i, dict) and isinstance(i.get("text"), str)) > REMOTE_MAX_CHARS:
        return 413, "too many keys in one post"
    try:  # a lone surrogate cannot reach tmux: refused here, before a number is taken
        for i in seq:
            if isinstance(i, dict) and isinstance(i.get("text"), str):
                i["text"].encode("utf-8")
    except UnicodeEncodeError:
        return 400, "text is not valid Unicode"
    from orch.remote.bridge_host.budgets import SlidingLimit  # a bridged request only: a local run never loads it
    with _DEVICE_LOCK:
        if n <= _DEVICE_LAST.get((device, page), 0):
            return 409, "post number already used"
        if not _DEVICE_RATE.setdefault(device, SlidingLimit(REMOTE_POSTS, REMOTE_WINDOW_MS)).take(
                time.monotonic_ns() // 1_000_000):
            return 429, "too many key posts: batch the keys"
        _DEVICE_LAST.pop((device, page), None)  # re-inserted last: the oldest go first
        _DEVICE_LAST[(device, page)] = n
        mine = [k for k in _DEVICE_LAST if k[0] == device]
        for k in mine[:max(0, len(mine) - MAX_PAGES_PER_DEVICE)]:
            del _DEVICE_LAST[k]
        while len(_DEVICE_LAST) > MAX_PAGES:
            del _DEVICE_LAST[next(iter(_DEVICE_LAST))]
    return None


@router.post("/terminals/{name}/keys")
async def keys(request: Request, name: str):
    if not strict_same_origin(request):
        return PlainTextResponse("cross-origin request refused", status_code=403)
    if _found(request, name) is None:
        return PlainTextResponse("no such terminal", status_code=404)
    try:
        data = await _json(request)
        origin = remote_origin(request)
        if origin is not None and (refusal := _device_keys_refusal(origin.device, data)) is not None:
            return PlainTextResponse(refusal[1], status_code=refusal[0])
        await asyncio.to_thread(terminals.send, name, data.get("seq") if isinstance(data, dict) else None)
    except OrchError as e:
        return PlainTextResponse(error_text(e), status_code=400)
    _wake(name)  # the open views of this session look again at once
    return Response(status_code=204)


@router.post("/terminals/{name}/size")
async def size(request: Request, name: str):
    if not strict_same_origin(request):
        return PlainTextResponse("cross-origin request refused", status_code=403)
    if _found(request, name) is None:
        return PlainTextResponse("no such terminal", status_code=404)
    try:
        data = await _json(request)
        cols, rows = int(data["cols"]), int(data["rows"])
    except (OrchError, KeyError, TypeError, ValueError):
        return PlainTextResponse("cols and rows must be numbers", status_code=400)
    await asyncio.to_thread(terminals.resize, name, cols, rows)
    _wake(name)
    return Response(status_code=204)


@router.post("/terminals/{name}/end")
def end(request: Request, name: str, ask: Annotated[str, Form()] = ""):
    if not strict_same_origin(request):
        return PlainTextResponse("cross-origin request refused", status_code=403)
    if _found(request, name) is None:
        return back("/terminals", err=f"no terminal named {name}")
    if ask:  # posted without JS: confirm on a page first
        return confirm_page(request, action=f"/terminals/{name}/end", fields=[], title=f"End {name}?",
                            body="The program running in it stops at once. Its conversation stays wherever the "
                                 "program keeps it.", confirm="End session", cancel_href=f"/terminals/{name}",
                            danger=True, nav="terminals")
    try:
        terminals.end(name)
    except OrchError as e:
        return back(f"/terminals/{name}", err=error_text(e))
    return back("/terminals", msg=f"Ended {name}")


@router.post("/terminals/new")
def new(request: Request):
    """A scratch session: the addon's harness (Claude Code for now) without a prompt, in the workspace root."""
    if not strict_same_origin(request):
        return PlainTextResponse("cross-origin request refused", status_code=403)
    if _off(request):
        return PlainTextResponse("not found", status_code=404)
    ws = request.app.state.ws
    settings = launch.load_settings()
    harness = terminals.settings(ws.root)["harness"]
    template = agent_start.harnesses(ws, settings).get(harness)
    if not template:
        return back("/terminals", err=f"unknown harness {harness!r}")
    argv = terminals.scratch_argv(template)
    try:
        name = terminals.free_name(ws, "scratch")
        launch.start(ws, name, argv, terminal="tmux", name=name, harness=harness, settings=settings)
    except OrchError as e:
        return back("/terminals", err=error_text(e))
    return back(f"/terminals/{name}", msg=f"Started {harness} in {name}")
