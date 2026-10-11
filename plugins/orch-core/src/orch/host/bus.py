"""The host's bus (§0.9, H6a): subscriptions and parked ``wait``s, on the event loop.

Each workspace worker hands its journal's new entries over as a :class:`~orch.host.journal.Batch`
(:meth:`Bus.ingest`, via ``call_soon_threadsafe``). The bus keeps a mirror of each journal's window (a :class:`Feed`)
and delivers it to every subscription; it never touches a store.

**Delivery** (:meth:`Bus.flush`), for one subscription, every time:

1. the subscription is live only once the ``res`` of its ``sub.open`` is queued (that ``res`` comes first);
2. **the principal is revalidated** against the workspace's current authentication snapshot: a grant that no longer
   verifies (expired, revoked, its person gone) ends it with ``grant.expired`` (``person.removed`` when the person is no
   longer a member), a person-class mapping that no longer holds with ``person.removed``. Queued items are dropped with
   it. A snapshot older than the workspace log is not trusted: the worker is asked to refresh it first;
3. nothing is sent while the connection has a write in flight whose ``res`` is not queued yet, so a write's ``res``
   precedes every ``evt`` with its events;
4. each pending entry is turned into items **for the viewer, now** (:meth:`Bus.items`): the workspace-log per-type
   rule with the viewer's current role, the ticket's visibility at the entry and now, ``gone`` and ``needs.changed``;
5. items go out in ``id`` order, at most 64 per ``evt``, never splitting one ``jpos``; ``evt.cursor`` is after the
   last ``jpos`` the frame completes, filtered positions included;
6. a slow reader: while the connection's write queue is more than half full nothing is sent; a backlog over
   :attr:`Bus.max_items` items or :attr:`Bus.max_bytes`, or blocked longer than :attr:`Bus.max_lag`, ends the
   subscription with ``overflow``.

**Ends.** ``closed`` (``sub.close``), ``grant.expired``, ``person.removed``, ``overflow``, ``reset`` (a journal rebuild:
a foreign append or a restore) and ``host.restarting`` (drain). An ended subscription sends one last
``evt {items: [], end}`` and is gone.

**``wait``** parks on the bus (:meth:`Bus.watch`): an entry of the ticket's log (or a rebuild) wakes it, and the
dispatcher looks again on the worker thread. Nothing sleeps on a worker thread.
"""

from __future__ import annotations

import asyncio
import json
import logging
import secrets
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from . import frames
from .errors import HostError
from .journal import WINDOW, Batch, Entry, Vis, encode_cursor, sees
from .transport import WRITE_QUEUE_BYTES, WRITE_QUEUE_FRAMES

if TYPE_CHECKING:
    from .service import Host
    from .transport import Session

__all__ = ["GRANT_TYPES", "PERSON_TYPES", "Bus", "Feed", "Subscription"]

LOG = logging.getLogger(__name__)

#: §0.8 ``activity.list``: workspace-log events every viewer with a person gets, whatever the member role
PERSON_TYPES = frozenset(
    {
        "workspace.created",
        "member.added",
        "member.removed",
        "role.changed",
        "device.added",
        "device.removed",
        "device.revoked",
        "policy.changed",
        "settings.changed",
        "addon.granted",
        "addon.disabled",
        "addon.purged",
        "restore",
        "invalid.acknowledged",
    }
)
#: only to a person-class owner and to the grant's own person (the ``grant.list`` rule)
GRANT_TYPES = frozenset({"grant.issued", "grant.revoked"})

MAX_ITEMS = 256
MAX_BYTES = 256 * 1024
MAX_LAG_SECONDS = 30.0
EVT_ITEMS = 64
PER_CONNECTION = 8
PER_UID = 64
TICK_SECONDS = 0.1


@dataclass
class Feed:
    """The bus's mirror of one workspace's journal window."""

    workspace_id: str
    epoch: str = ""
    head: int = 0
    window: deque[Entry] = field(default_factory=lambda: deque(maxlen=WINDOW))
    #: each ticket's visibility now (as of the last batch)
    vis: dict[str, Vis] = field(default_factory=dict)

    @property
    def floor(self) -> int:
        return self.window[0].jpos - 1 if self.window else self.head

    def after(self, jpos: int) -> list[Entry]:
        """The entries after ``jpos`` (``jpos`` at least :attr:`floor`). ``jpos`` is contiguous in the window."""
        if not self.window or jpos >= self.head:
            return []
        start = max(0, jpos - self.window[0].jpos + 1)
        return [self.window[i] for i in range(start, len(self.window))]


@dataclass(eq=False)
class Subscription:
    id: str
    session: Session
    request: frames.Request  # the sub.open request: no evt before its res
    workspace_id: str
    topics: frozenset[str]
    tickets: frozenset[str]  # uids of the ticket:<ref> topics
    principal: str
    column: str  # P, G or N at open
    person: str | None
    grant: str | None  # the presented grant (column G), kept to reverify it; never sent
    grant_id: str | None
    uid: int
    pos: int  # the last jpos delivered or passed
    blocked_since: float | None = None
    ended: bool = False

    @property
    def live(self) -> bool:
        return self.request.answered or self.request in self.session.answering

    def cursor(self, epoch: str) -> str:
        return encode_cursor(self.workspace_id, self.principal, epoch, self.pos)


class Bus:
    def __init__(self, host: Host, *, clock: Callable[[], float] = time.monotonic) -> None:
        self.host = host
        self.clock = clock
        self.feeds: dict[str, Feed] = {}
        self.subs: dict[str, Subscription] = {}
        self.watchers: dict[tuple[str, str | None], set[asyncio.Event]] = {}
        self.max_items, self.max_bytes, self.max_lag = MAX_ITEMS, MAX_BYTES, MAX_LAG_SECONDS
        #: test seam: while set, nothing is delivered (items queue; revalidation and overflow still apply)
        self.hold = False
        self._kicked = False
        self._ticker: asyncio.Task[None] | None = None
        self._refreshing: set[str] = set()

    # -- lifecycle
    def start(self) -> None:
        if self._ticker is None:
            self._ticker = asyncio.get_running_loop().create_task(self._tick())

    async def stop(self) -> None:
        if self._ticker is not None:
            self._ticker.cancel()
            await asyncio.gather(self._ticker, return_exceptions=True)
            self._ticker = None

    async def _tick(self) -> None:
        while True:
            await asyncio.sleep(TICK_SECONDS)
            try:
                for sub in [s for s in self.subs.values() if s.session.closed]:
                    self.end(sub, "closed")  # its connection is gone: free its quota slot
                if any(not s.ended and s.pos < self._head(s) for s in self.subs.values()) or self._kicked:
                    self.flush()
            except Exception:  # noqa: BLE001 - the bus must keep running
                LOG.exception("subscription delivery failed")

    def _head(self, sub: Subscription) -> int:
        feed = self.feeds.get(sub.workspace_id)
        return feed.head if feed is not None else 0

    def kick(self) -> None:
        """Deliver soon (after the current callback): a write answered, a batch arrived, a sub went live."""
        if self._kicked:
            return
        self._kicked = True
        try:
            asyncio.get_running_loop().call_soon(self.flush)
        except RuntimeError:
            self._kicked = False

    # -- from the workers
    def publisher(self, workspace_id: str) -> Callable[[Batch], None]:
        """The ``publish`` callback a workspace worker uses: hands the batch to the loop."""
        loop = asyncio.get_running_loop()

        def publish(batch: Batch) -> None:
            try:
                loop.call_soon_threadsafe(self.ingest, batch)
            except RuntimeError:  # the loop is closed: nobody listens any more
                pass

        return publish

    def ingest(self, batch: Batch) -> None:
        feed = self.feeds.get(batch.workspace_id)
        if batch.reset:
            self.end_all("reset", batch.workspace_id)  # a rebuilt journal: every subscription takes a snapshot
        if feed is None or batch.reset:
            feed = Feed(batch.workspace_id)
            self.feeds[batch.workspace_id] = feed
        if batch.reset:
            feed.vis = dict(batch.vis)
            feed.window = deque(batch.entries, maxlen=WINDOW)
        else:
            feed.vis.update(batch.vis)
            feed.window.extend(batch.entries)
        feed.epoch, feed.head = batch.epoch, batch.head
        logs = {e.log for e in batch.entries}
        for (wid, log), events in list(self.watchers.items()):
            if wid == batch.workspace_id and (batch.reset or log is None or log in logs):
                for event in events:
                    event.set()
        self.kick()

    # -- wait
    def watch(self, workspace_id: str, log: str | None) -> asyncio.Event:
        """An event set when the next batch for ``log`` (``None``: any log) of the workspace arrives."""
        event = asyncio.Event()
        self.watchers.setdefault((workspace_id, log), set()).add(event)
        return event

    def unwatch(self, workspace_id: str, log: str | None, event: asyncio.Event) -> None:
        events = self.watchers.get((workspace_id, log))
        if events is not None:
            events.discard(event)
            if not events:
                del self.watchers[(workspace_id, log)]

    # -- sub.open / sub.close
    def open(
        self,
        session: Session,
        request: frames.Request,
        workspace_id: str,
        *,
        topics: frozenset[str],
        tickets: frozenset[str],
        principal: str,
        column: str,
        person: str | None,
        grant: str | None,
        grant_id: str | None,
        pos: int,
    ) -> Subscription:
        """Register a subscription whose live boundary the worker fixed at ``pos`` (§0.11 quotas apply)."""
        if self.host.draining:
            raise self.host.restarting()
        for old in [s for s in self.subs.values() if s.session.closed]:
            self.end(old, "closed")
        mine = [s for s in self.subs.values() if s.session is session]
        if len(mine) >= PER_CONNECTION:
            raise HostError("quota.host", f"at most {PER_CONNECTION} subscriptions per connection")
        if sum(1 for s in self.subs.values() if s.session.peer.uid == session.peer.uid) >= PER_UID:
            raise HostError("quota.host", f"at most {PER_UID} subscriptions per UID")
        feed = self.feeds.get(workspace_id)
        if feed is None or pos < feed.floor:
            raise HostError("sub.reset", "the journal moved on; take a snapshot and open again")
        while True:
            sid = "sub_" + secrets.token_hex(8)
            if sid not in self.subs:
                break
        sub = Subscription(
            sid, session, request, workspace_id, topics, tickets, principal, column, person, grant, grant_id,
            session.peer.uid, pos,
        )  # fmt: skip
        self.subs[sid] = sub
        session.subscriptions += 1
        return sub

    def close(self, session: Session, sid: str) -> bool:
        """``sub.close``: ``False`` for a subscription this connection does not hold (already ended included)."""
        sub = self.subs.get(sid)
        if sub is None or sub.session is not session:
            return False
        # after the res of sub.close (the caller answers first): one evt {end:"closed"}
        asyncio.get_running_loop().call_soon(self.end, sub, "closed")
        return True

    def cursor(self, sub: Subscription) -> str:
        feed = self.feeds.get(sub.workspace_id)
        return sub.cursor(feed.epoch if feed is not None else "")

    def end(self, sub: Subscription, reason: str) -> None:
        if sub.ended:
            return
        sub.ended = True
        self.subs.pop(sub.id, None)
        sub.session.subscriptions = max(0, sub.session.subscriptions - 1)
        if sub.session.closed:
            return
        frame = {"t": "evt", "sub": sub.id, "cursor": self.cursor(sub), "items": [], "end": reason}
        try:
            sub.session.send(frame)
        except frames.FrameError as exc:  # a reader too slow to take even the end: the connection closes
            sub.session.failure = exc

    def end_all(self, reason: str, workspace_id: str | None = None) -> None:
        for sub in list(self.subs.values()):
            if workspace_id is None or sub.workspace_id == workspace_id:
                self.end(sub, reason)

    # -- delivery
    def flush(self) -> None:
        self._kicked = False
        for sub in list(self.subs.values()):
            if not sub.ended:
                self._flush(sub)

    def _service(self, workspace_id: str) -> Any:
        return self.host.services.get(workspace_id)

    def principal_end(self, sub: Subscription, auth: Any, now: float) -> str | None:
        """The end reason if the subscription's principal no longer holds (§0.9 revalidation), else ``None``."""
        if sub.column == "G":
            verified = auth.verify(sub.grant, now)
            if verified.state == "verified" and verified.grant is not None and verified.grant.id == sub.grant_id:
                return None
            return "person.removed" if sub.person not in auth.roles else "grant.expired"
        if sub.column == "P":
            registry, pins = self.host.registry, self.host.pins
            mapped = registry.mapped_person(sub.uid, sub.workspace_id)
            if mapped == sub.person and not pins.pinned(sub.uid) and auth.person_ok(mapped, now):
                return None
            return "person.removed"
        return None

    def _flush(self, sub: Subscription) -> None:
        session = sub.session
        if session.closed:
            self.end(sub, "closed")
            return
        if not sub.live:
            return
        feed = self.feeds.get(sub.workspace_id)
        service = self._service(sub.workspace_id)
        if feed is None or service is None or (sub.pos >= feed.head and sub.pos >= feed.floor):
            return  # nothing to deliver: the next entry revalidates
        auth = service.auth
        if auth is None or not auth.fresh(service.entry.root):
            self._refresh(service)  # never deliver on a snapshot older than the log: the worker refreshes it
            return
        reason = self.principal_end(sub, auth, service.clock())
        if reason is not None:
            self.end(sub, reason)  # queued items are dropped with it
            return
        if sub.pos < feed.floor:
            self.end(sub, "reset")
            return
        pending = feed.after(sub.pos)
        if not pending:
            return
        owner = sub.column == "P" and auth.owner(sub.person)
        transport_full = not self._room(session)
        if self.hold or transport_full or self._write_in_flight(session):
            if transport_full and sub.blocked_since is None:
                sub.blocked_since = self.clock()
            if self._overflowing(sub, feed, pending, owner):
                self.end(sub, "overflow")
            return
        sub.blocked_since = None
        self._deliver(sub, feed, pending, owner)

    def _refresh(self, service: Any) -> None:
        wid = service.entry.workspace_id
        if wid in self._refreshing:
            return
        self._refreshing.add(wid)

        def done(_: Any) -> None:
            self._refreshing.discard(wid)  # the next batch or tick delivers

        try:
            future = service.submit(lambda: (service.refresh(), service.sync(check=True)))
        except RuntimeError:  # the worker is shut down
            self._refreshing.discard(wid)
            return
        asyncio.wrap_future(future).add_done_callback(done)

    @staticmethod
    def _room(session: Session) -> bool:
        return session.queue.qsize() < WRITE_QUEUE_FRAMES // 2 and session.queued_bytes < WRITE_QUEUE_BYTES // 2

    @staticmethod
    def _write_in_flight(session: Session) -> bool:
        return any(r.phase == "write" and not r.answered and r not in session.answering for r in list(session.tasks))

    def items(self, sub: Subscription, feed: Feed, entry: Entry, owner: bool) -> list[dict[str, Any]]:
        """The items one entry yields for this subscription's viewer now, in ``id`` order (e < g < n)."""
        out: list[dict[str, Any]] = []
        person = sub.person
        if entry.log == "workspace":
            if "workspace" in sub.topics and self._workspace_visible(sub, entry, owner):
                out.append(entry.item())
        elif "tickets" in sub.topics or entry.log in sub.tickets:
            before, after = sees(person, entry.before), sees(person, entry.after)
            now = sees(person, feed.vis.get(entry.log))
            hidden = before and not after  # the ticket became invisible to this viewer at this jpos
            if (after and now) or hidden:
                item = entry.item()
                if after and not before and entry.before is not None:
                    item["type"] = "visibility.changed"
                out.append(item)
            if hidden:  # the one item exempt from the filter: the uid the viewer already knew
                out.append({"id": f"{entry.jpos}.g", "jpos": entry.jpos, "uid": entry.log, "gone": True})
        if entry.needs and "needs" in sub.topics:
            out.append({"id": f"{entry.jpos}.n", "jpos": entry.jpos, "type": "needs.changed"})
        return out

    @staticmethod
    def _workspace_visible(sub: Subscription, entry: Entry, owner: bool) -> bool:
        """§0.9 (v1.5): ``activity.list``'s per-type rule, with the viewer's role now."""
        if sub.person is None:
            return False
        if entry.type in GRANT_TYPES:
            return owner or (entry.grant_person is not None and entry.grant_person == sub.person)
        return entry.type in PERSON_TYPES

    def _overflowing(self, sub: Subscription, feed: Feed, pending: list[Entry], owner: bool) -> bool:
        if sub.blocked_since is not None and self.clock() - sub.blocked_since > self.max_lag:
            return True
        count = size = 0
        for entry in pending:
            for item in self.items(sub, feed, entry, owner):
                count += 1
                size += len(json.dumps(item, separators=(",", ":")))
                if count > self.max_items or size > self.max_bytes:
                    return True
        return False

    def _deliver(self, sub: Subscription, feed: Feed, pending: list[Entry], owner: bool) -> None:
        batch: list[dict[str, Any]] = []
        upto = sub.pos
        for entry in pending:
            items = self.items(sub, feed, entry, owner)
            if batch and len(batch) + len(items) > EVT_ITEMS:
                if not self._send(sub, feed, batch, upto):
                    return
                batch = []
            batch.extend(items)
            upto = entry.jpos
        if batch:
            self._send(sub, feed, batch, upto)
        else:
            sub.pos = upto  # filtered positions advance the cursor; the next evt carries it

    def _send(self, sub: Subscription, feed: Feed, items: list[dict[str, Any]], upto: int) -> bool:
        if not self._room(sub.session):
            return False
        frame = {
            "t": "evt",
            "sub": sub.id,
            "cursor": encode_cursor(sub.workspace_id, sub.principal, feed.epoch, upto),
            "items": items,
        }
        try:
            sub.session.send(frame)
        except frames.FrameError as exc:
            sub.session.failure = exc
            return False
        sub.pos = upto
        return True
