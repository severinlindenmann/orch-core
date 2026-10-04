from __future__ import annotations

import asyncio
import hashlib
import os
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse

router = APIRouter()


def _under(path: Path, base: Path) -> bool:
    try:
        path.relative_to(base)
        return True
    except ValueError:
        return False


def watch_filter(ws):
    tickets, artifacts = ws.tickets_dir.resolve(), ws.artifacts_dir.resolve()
    events = (ws.state_dir / "events.jsonl").resolve()
    marker = (ws.state_dir / "addons" / "changed").resolve()  # rewritten when an addon snapshot changes

    def accept(change, path: str) -> bool:
        p = Path(path).resolve()
        if p == events or p == marker:
            return True
        if p.name.startswith("."):
            return False  # temp files from atomic writes
        return _under(p, tickets) or _under(p, artifacts)

    return accept


def live_version(ws) -> str:
    """A cheap fingerprint of what the live stream watches: the event log, the addon change marker and every
    ticket file.

    Pages carry it in <html data-version>; a tab that comes back from the background compares it
    with the hello frame of its new /events stream and reloads only when something changed. Within a page
    request it reuses the stats the ticket scan already took (store.stat) and is computed once."""
    from orch.core import store

    return store.memo(ws, "live-version", lambda: _live_version(ws))


def _live_version(ws) -> str:
    from orch.core import store

    home = os.fspath(ws.home)
    tickets = os.fspath(ws.tickets_dir)
    found = []
    for dirpath, dirnames, filenames in os.walk(tickets):
        dirnames.sort()
        found += [os.path.join(dirpath, n) for n in filenames if n.endswith(".md")]
    found.sort(key=lambda f: os.path.relpath(f, tickets).split(os.sep))
    parts = []
    for path in [os.fspath(ws.state_dir / "events.jsonl"), os.fspath(ws.state_dir / "addons" / "changed"), *found]:
        try:
            st = store.stat(path)
        except OSError:
            continue
        parts.append(f"{Path(os.path.relpath(path, home)).as_posix()}:{st.st_size}:{st.st_mtime_ns}")
    return f"{len(parts)}-{hashlib.sha1(chr(10).join(parts).encode()).hexdigest()[:12]}"


def _awatch():
    from watchfiles import awatch

    return awatch


class ChangeHub:
    """One file watcher per workspace and event loop, shared by every open /events stream.

    A watcher per stream held one worker thread each, and kept holding it for up to a heartbeat
    after the browser had left the page; a few tabs and page switches used up the thread pool that
    the normal page routes share, so pages hung until those threads came back."""

    def __init__(self, ws):
        self.ws = ws
        self.subscribers: set[asyncio.Queue] = set()
        self.task: asyncio.Task | None = None

    def subscribe(self) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue(maxsize=1)  # one pending change is enough to reload
        self.subscribers.add(queue)
        if self.task is None or self.task.done():
            self.task = asyncio.ensure_future(self._watch())
        return queue

    def unsubscribe(self, queue: asyncio.Queue) -> None:
        self.subscribers.discard(queue)
        if not self.subscribers and self.task is not None:
            self.task.cancel()
            self.task = None

    async def _watch(self) -> None:
        ws = self.ws
        paths = [ws.tickets_dir, ws.artifacts_dir, ws.state_dir]
        async for _changes in _awatch()(*paths, watch_filter=watch_filter(ws), rust_timeout=1000):
            for queue in list(self.subscribers):
                if queue.empty():
                    queue.put_nowait(True)


_HUBS: dict[tuple[str, int], ChangeHub] = {}


def _hub(ws) -> ChangeHub:
    key = (str(ws.home), id(asyncio.get_running_loop()))
    hub = _HUBS.get(key)
    if hub is None:
        hub = _HUBS[key] = ChangeHub(ws)
    return hub


def subscriber_count(ws) -> int:
    """How many live-update streams are open for this workspace (over all event loops)."""
    home = str(ws.home)
    return sum(len(hub.subscribers) for (key, _), hub in list(_HUBS.items()) if key == home)


async def change_stream(ws, *, timeout: float | None = None, heartbeat_ms: int = 15000):
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout if timeout else None
    hub = _hub(ws)
    queue = hub.subscribe()
    try:
        yield f": connected\n\nevent: hello\ndata: {live_version(ws)}\n\n"
        while True:
            wait = heartbeat_ms / 1000
            if deadline is not None:
                wait = max(0.0, min(wait, deadline - loop.time()))
            try:
                await asyncio.wait_for(queue.get(), timeout=wait)
                yield "event: change\ndata: {}\n\n"
            except asyncio.TimeoutError:
                yield ": ping\n\n"
            if deadline is not None and loop.time() >= deadline:
                break
    finally:
        hub.unsubscribe(queue)


@router.get("/events")
async def events(request: Request, timeout: float | None = None):
    return StreamingResponse(change_stream(request.app.state.ws, timeout=timeout), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
