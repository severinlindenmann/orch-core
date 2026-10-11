"""One upstream subscription per UUID, no downstream replay, bounded per-tab queues."""

import asyncio
import json
from contextlib import suppress
from dataclasses import dataclass, field

from starlette.responses import StreamingResponse

from .errors import HostError, fail


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
        self.link = None
        self.ready = False
        self.seen = set()

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
        self.link = self.manager.gateway.enabled(self.ws)
        # Use the earlier cursor so changes between the two snapshots are replayed.
        snapshot = self.link.call("ticket.query", {"limit": 1})
        self.link.call("needs")
        opened = self.link.call(
            "sub.open", {"topics": ["workspace", "tickets", "needs"], "after": snapshot["sub_cursor"]}
        )
        self.sub = opened["sub"]
        self.seen.clear()

    def close_sub(self):
        if self.sub and self.link:
            with suppress(HostError, OSError):
                self.link.call("sub.close", {"sub": self.sub})
        self.sub = None

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
        try:
            while True:
                try:
                    await self.open()
                    if self.ready:
                        self.emit("resync", {})
                    else:
                        self.ready = True
                        self.emit("ready", {})
                    while True:
                        frame = await asyncio.to_thread(self.link.next_event)
                        if frame is None:
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
                        if items:
                            self.emit("items", items)
                except HostError as exc:
                    if exc.error["code"] in {"peer.denied", "addon.disabled", "not_found"}:
                        self.emit("revoked", {})
                        return
                    self.emit("resync", {})
                    await asyncio.sleep(1)
                finally:
                    await asyncio.to_thread(self.close_sub)
        finally:
            self.ready = False


class Live:
    def __init__(self, gateway, auth):
        self.gateway, self.auth = gateway, auth
        self.workspaces = {}

    def attach(self, ws, token, last=None):
        if sum(t.token == token for w in self.workspaces.values() for t in w.tabs) >= 16:
            raise fail("quota.host")
        workspace = self.workspaces.setdefault(ws, WorkspaceLive(self, ws))
        tab = Tab(token)
        workspace.tabs.add(tab)
        if workspace.ready:
            tab.queue.put_nowait(("ready", {}, workspace.latest))
        if last and last != workspace.latest:
            tab.queue.put_nowait(("resync", {}, workspace.latest))
        if workspace.task is None or workspace.task.done():
            workspace.task = asyncio.create_task(workspace.run())
        return workspace, tab

    def checked(self, ws, items):
        """Recheck at drain time, not enqueue time. Only gone bypasses visibility."""
        who = self.gateway.call(ws, "whoami")
        members = self.gateway.call(ws, "member.list")["members"]
        if who.get("person") not in {m["person"] for m in members}:
            raise fail("peer.denied", exit=3)
        if not items:
            return []
        visible = set()
        page = None
        while True:
            args = {"limit": 200}
            if page:
                args["page"] = page
            data = self.gateway.call(ws, "ticket.query", args)
            visible.update(r["uid"] for r in data["rows"])
            page = data["next_page"]
            if not page:
                break
        return [i for i in items if i.get("gone") or not i.get("uid") or i["uid"] in visible]

    async def stream(self, workspace, tab):
        heartbeat = asyncio.get_running_loop().time()
        try:
            while self.auth.valid(tab.token, touch=False):
                try:
                    event, data, event_id = await asyncio.wait_for(tab.queue.get(), 0.25)
                except TimeoutError:
                    if asyncio.get_running_loop().time() - heartbeat >= 15:
                        # Also check membership while a workspace is quiet.
                        try:
                            await asyncio.to_thread(self.checked, workspace.ws, [])
                        except HostError:
                            yield sse("revoked", {}, workspace.latest)
                            return
                        yield ": heartbeat\n\n"
                        heartbeat = asyncio.get_running_loop().time()
                    continue
                if not self.auth.valid(tab.token, touch=False):
                    return
                if event == "items":
                    try:
                        data = await asyncio.to_thread(self.checked, workspace.ws, data)
                    except HostError as exc:
                        event, data = (
                            (
                                "revoked"
                                if exc.error["code"] in {"peer.denied", "addon.disabled", "not_found"}
                                else "resync"
                            ),
                            {},
                        )
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
                        workspace.task = asyncio.create_task(workspace.run())

    async def close(self):
        tasks = [w.task for w in self.workspaces.values() if w.task]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
