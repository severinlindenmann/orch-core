"""One upstream subscription per UUID, no downstream replay, bounded per-tab queues."""

import asyncio
import contextvars
import json
import threading
import time
from contextlib import suppress
from dataclasses import dataclass, field

from starlette.responses import StreamingResponse

from .adapters import runs as run_items
from .errors import HostError, fail

VISIBILITY_TTL = 1.0
HEARTBEAT = 15.0
REVOKED = {"peer.denied", "addon.disabled", "not_found"}
BACKOFF = (1, 2, 5, 10)
SEED_PAGES = 50
DIRECT_CHECKS = 20
MEMBER_EVENTS = ("member.", "role.", "device.", "grant.")


def sse(event, data, event_id):
    if event == "ready":
        data = {"id": event_id}
    return f"id: {event_id}\nevent: {event}\ndata: {json.dumps(data, separators=(',', ':'))}\n\n"


class SessionStream(StreamingResponse):
    """Revocation also interrupts an ASGI send blocked behind a slow browser."""

    def __init__(self, content, auth, token):
        super().__init__(
            content, media_type="text/event-stream", headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"}
        )
        self.auth, self.token = auth, token

    async def __call__(self, scope, receive, send):
        async def watch():
            while self.auth.valid(self.token, touch=False):
                await asyncio.sleep(0.25)

        response = asyncio.create_task(super().__call__(scope, receive, send))
        watcher = asyncio.create_task(watch())
        try:
            done, _ = await asyncio.wait([response, watcher], return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                await task
        finally:
            response.cancel()
            watcher.cancel()
            await asyncio.gather(response, watcher, return_exceptions=True)
            await self.body_iterator.aclose()


@dataclass(eq=False)
class Tab:
    token: str
    queue: asyncio.Queue = field(default_factory=lambda: asyncio.Queue(maxsize=256))
    dropped: bool = False


class WorkspaceLive:
    def __init__(self, manager, ws):
        self.manager, self.ws = manager, ws
        self.tabs = set()
        self.serial = 0
        self.task = None
        self.sub = None
        self.run_sub = None  # run.sub (M7a): the person's runs, opened before the page lists them
        self.link = None
        self.ready = False
        self.seen = set()
        self.visible = set()
        self.partial = True
        self.outage = False

    @property
    def latest(self):
        return f"{self.manager.auth.instance}:{self.ws}:{self.serial}"

    def emit(self, event, data):
        self.serial += 1
        for tab in list(self.tabs):
            if tab.dropped:
                continue
            if tab.queue.full():
                while not tab.queue.empty():
                    tab.queue.get_nowait()
                tab.dropped = True
                tab.queue.put_nowait(("resync", {}, self.latest))
            else:
                tab.queue.put_nowait((event, data, self.latest))

    def snapshot_open(self):
        self.manager.invalidate(self.ws)
        self.link = self.manager.gateway.enabled(self.ws)
        # Runs (D-P2-37): subscribe first; the page lists (`GET .../runs`, a full run.list) after `ready`/`resync`.
        served = self.link.supports("run.sub", "run/1") is True
        self.run_sub = self.link.call("run.sub")["sub"] if served else None
        # Use the earlier cursor so changes between the two snapshots are replayed.
        snapshot = self.link.call("ticket.query", {"limit": 200})
        # Remember which tickets this person can see, so a host `gone` is only forwarded for those.
        visible = {r["uid"] for r in snapshot.get("rows", [])}
        page, pages = snapshot.get("next_page"), 1
        while page and pages < SEED_PAGES:
            data = self.link.call("ticket.query", {"limit": 200, "page": page})
            visible.update(r["uid"] for r in data.get("rows", []))
            page, pages = data.get("next_page"), pages + 1
        self.visible, self.partial = visible, bool(page)
        self.link.call("needs")
        opened = self.link.call(
            "sub.open", {"topics": ["workspace", "tickets", "needs"], "after": snapshot["sub_cursor"]}
        )
        self.sub = opened["sub"]
        self.seen.clear()

    def admit(self, items):
        """Forward every host `gone`; invalidate shared visibility on gone and membership events."""
        out = []
        for item in items:
            if item.get("gone"):
                # The host is the authority: it sends gone only for tickets that were visible (contract 0.9).
                self.visible.discard(item.get("uid"))
                self.manager.invalidate(self.ws)
            elif str(item.get("type", "")).startswith(MEMBER_EVENTS):
                self.manager.invalidate(self.ws)
            out.append(item)
        return out

    def close_sub(self):
        for sub in (self.sub, self.run_sub):
            if sub and self.link:
                with suppress(HostError, OSError):
                    self.link.call("sub.close", {"sub": sub})
        self.sub = self.run_sub = None

    async def open(self):
        # Cancelling to_thread does not stop its socket call. Finish opening before cleanup
        # so a tab disconnect during the snapshot cannot leak an upstream subscription.
        opening = asyncio.create_task(asyncio.to_thread(self.snapshot_open))
        try:
            await asyncio.shield(opening)
        except asyncio.CancelledError:
            with suppress(HostError):
                await opening
            raise

    async def run(self):
        delay = 0
        try:
            while True:
                try:
                    await self.open()
                    delay = 0
                    if self.ready:
                        self.emit("resync", {})
                    else:
                        self.ready = True
                        self.emit("ready", {})
                    self.outage = False
                    beat = time.monotonic()
                    while True:
                        if time.monotonic() - beat >= HEARTBEAT:
                            # One membership check per workspace, shared by every tab.
                            await asyncio.to_thread(self.manager.checked, self.ws, [])
                            beat = time.monotonic()
                        frame = await asyncio.to_thread(self.link.next_event)
                        if frame is None:
                            continue
                        if self.run_sub is not None and frame.get("sub") == self.run_sub:
                            if frame.get("end"):
                                break  # resync: the page replaces its runs with a full run.list
                            runs = run_items.items(frame.get("items", []))
                            if runs:
                                self.emit("runs", runs)
                            continue
                        if frame.get("sub", self.sub) != self.sub:
                            continue
                        end = frame.get("end")
                        if end in {"person.removed", "grant.expired"}:
                            self.emit("revoked", {})
                            return
                        if end or frame.get("invalidated"):
                            break
                        items = [i for i in frame.get("items", []) if i["id"] not in self.seen]
                        self.seen.update(i["id"] for i in items)
                        if len(self.seen) > 30000:
                            self.seen = {i["id"] for i in items}
                        items = self.admit(items)
                        if items:
                            self.emit("items", items)
                except HostError as exc:
                    self.manager.invalidate(self.ws)
                    if exc.error["code"] in REVOKED:
                        self.emit("revoked", {})
                        return
                    # An outage is not a revocation: tell the tabs once, then retry with backoff.
                    if not self.outage:
                        self.outage = True
                        self.emit("resync", {})
                    await asyncio.sleep(BACKOFF[min(delay, len(BACKOFF) - 1)])
                    delay += 1
                finally:
                    await asyncio.to_thread(self.close_sub)
        finally:
            self.ready = False


class Live:
    def __init__(self, gateway, auth):
        self.gateway, self.auth = gateway, auth
        self.workspaces = {}
        self.cache = {}
        self.vis_lock = threading.Lock()
        self.terminals = None  # open terminal sockets of a session (terminals.Streams.open), counted with the tabs

    def invalidate(self, ws):
        self.cache.pop(ws, None)

    def tabs_of(self, token):
        return sum(t.token == token for w in self.workspaces.values() for t in w.tabs)

    def attach(self, ws, token, last=None):
        # Open streams per session: SSE tabs and terminal sockets together (`terminals`, set by the app).
        if self.tabs_of(token) + (self.terminals(token) if self.terminals else 0) >= 16:
            raise fail("quota.host")
        workspace = self.workspaces.setdefault(ws, WorkspaceLive(self, ws))
        tab = Tab(token)
        workspace.tabs.add(tab)
        if workspace.ready:
            tab.queue.put_nowait(("ready", {}, workspace.latest))
        if last and last != workspace.latest:
            tab.queue.put_nowait(("resync", {}, workspace.latest))
        if workspace.task is None or workspace.task.done():
            workspace.task = asyncio.create_task(
                workspace.run(), context=contextvars.Context()
            )  # not the request's memo
        return workspace, tab

    def checked(self, ws, items):
        try:
            return self._checked(ws, items)
        except HostError:
            self.invalidate(ws)
            raise

    def _checked(self, ws, items):
        """Recheck at drain time, shared by every tab of this person for a short TTL.

        The host call count depends on the batch, never on the number of tabs.
        Only `gone` bypasses visibility (it was already filtered once per workspace).
        """
        with self.vis_lock:
            now = time.monotonic()
            entry = self.cache.get(ws)
            if entry is None or now - entry["at"] > VISIBILITY_TTL:
                entry = self.cache[ws] = {"at": now, "member": False, "tickets": {}}
            link = None
            if not entry["member"]:
                link = self.gateway.enabled(ws)
                # The connection's person was pinned at connect; the roster says if they still belong.
                members = link.call("member.list")["members"]
                if link.welcome["peer"]["person"] not in {m["person"] for m in members}:
                    raise fail("peer.denied", exit=3)
                entry["member"] = True
            if not items:
                entry["at"] = time.monotonic()
                return []
            tickets = entry["tickets"]
            wanted = {i["uid"]: i.get("key") for i in items if not i.get("gone") and i.get("uid")}
            missing = {uid: key for uid, key in wanted.items() if uid not in tickets}
            if missing:
                link = link or self.gateway.enabled(ws)
                if len(missing) <= DIRECT_CHECKS:
                    try:
                        for uid, key in missing.items():
                            try:
                                got = link.call("ticket.get", {"ref": key, "log_limit": 1}) if key else None
                                tickets[uid] = bool(key) and (got or {}).get("doc", {}).get("uid") == uid
                            except HostError as exc:
                                if exc.error["code"] != "not_found":
                                    raise
                                tickets[uid] = False
                    except HostError as exc:
                        if exc.error["code"] != "op.unsupported":
                            raise
                        missing = {uid: None for uid in wanted if uid not in tickets}
                if missing and any(uid not in tickets for uid in missing):
                    visible, page = set(), None
                    while True:
                        args = {"limit": 200, **({"page": page} if page else {})}
                        data = link.call("ticket.query", args)
                        visible.update(r["uid"] for r in data["rows"])
                        page = data["next_page"]
                        if not page:
                            break
                    for uid in missing:
                        tickets.setdefault(uid, uid in visible)
            entry["at"] = time.monotonic()  # the TTL starts when the answer is complete
            allowed = [i for i in items if i.get("gone") or not i.get("uid") or tickets.get(i["uid"])]
        live = self.workspaces.get(ws)
        if live is not None:
            live.visible.update(i["uid"] for i in allowed if i.get("uid") and not i.get("gone"))
        return allowed

    async def stream(self, workspace, tab):
        heartbeat = asyncio.get_running_loop().time()
        try:
            while self.auth.valid(tab.token, touch=False):
                try:
                    event, data, event_id = await asyncio.wait_for(tab.queue.get(), 0.25)
                except TimeoutError:
                    if asyncio.get_running_loop().time() - heartbeat >= 15:
                        # The workspace task owns the membership check.
                        yield ": heartbeat\n\n"
                        heartbeat = asyncio.get_running_loop().time()
                    continue
                if not self.auth.valid(tab.token, touch=False):
                    return
                if event == "items":
                    try:
                        data = await asyncio.to_thread(self.checked, workspace.ws, data)
                    except HostError as exc:
                        event, data = ("revoked" if exc.error["code"] in REVOKED else "resync"), {}
                    else:
                        if not data:
                            continue
                if not self.auth.valid(tab.token, touch=False):
                    return
                if tab.dropped:
                    event, data, event_id = "resync", {}, workspace.latest
                yield sse(event, data, event_id)
                if event == "revoked" or tab.dropped:
                    return
        finally:
            workspace.tabs.discard(tab)
            if not workspace.tabs and workspace.task:
                old_task = workspace.task
                workspace.ready = False
                old_task.cancel()
                with suppress(asyncio.CancelledError):
                    await old_task
                if workspace.task is old_task:
                    workspace.task = None
                    # A tab may have attached while sub.close was finishing.
                    if workspace.tabs:
                        workspace.task = asyncio.create_task(
                            workspace.run(), context=contextvars.Context()
                        )  # not the request's memo

    async def close(self):
        tasks = [w.task for w in self.workspaces.values() if w.task]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
