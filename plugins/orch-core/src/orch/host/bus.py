"""The host's bus (§0.9, H6a): subscriptions and parked ``wait``s, on the event loop.

Each workspace worker hands its journal's new entries over as a :class:`~orch.host.journal.Batch`
(:meth:`Bus.ingest`, via ``call_soon_threadsafe``). The bus keeps a mirror of each journal's window (a :class:`Feed`)
and delivers it to every subscription; it never touches a store.

**Delivery** (:meth:`Bus.flush`), for one subscription, every time:

1. the subscription is live only once the ``res`` of its ``sub.open`` is queued (that ``res`` comes first);
2. **the principal is revalidated** against the workspace's current authentication snapshot: a grant that no longer
   verifies (expired, revoked, its person gone) ends it with ``grant.expired`` (``person.removed`` when the person is no
   longer a member), a person-class mapping that no longer holds with ``person.removed``. Queued items are dropped with
   it. A snapshot older than the workspace log is not trusted: the worker is asked to refresh it first (at most once a
   second per workspace); a workspace with no snapshot or no journal ends its subscriptions with ``reset``;
3. nothing is sent while the connection has a write in flight whose ``res`` is not queued yet, so a write's ``res``
   precedes every ``evt`` with its events;
4. each pending entry is turned into items **for the viewer, now** (:meth:`Bus.items`): the workspace-log per-type
   rule with the viewer's current role; an event item only when its ticket is visible after the event **and** now;
   ``gone`` (exempt from the filter) when the viewer saw the ticket at the previous ``jpos`` and not after it, never
   together with the event that hid it; ``needs.changed``;
5. items go out in ``id`` order, at most 64 per ``evt``, never splitting one ``jpos``; ``evt.cursor`` is after the
   last ``jpos`` the frame completes, filtered positions included;
6. a slow reader: while the connection's write queue is more than half full nothing is sent. The backlog is counted
   incrementally (each pending entry is rendered once while blocked), and a backlog over :attr:`Bus.max_items` items
   or :attr:`Bus.max_bytes`, or blocked longer than :attr:`Bus.max_lag`, ends the subscription with ``overflow``.

**Ends.** ``closed`` (``sub.close``), ``grant.expired``, ``person.removed``, ``overflow``, ``reset`` (a journal rebuild:
a foreign append or a restore) and ``host.restarting`` (drain). An ended subscription sends one last
``evt {items: [], end}`` and is gone.

**``wait``** parks on the bus (:meth:`Bus.watch`): only an entry of its ticket whose type can answer it, a workspace
entry that can refuse it (a revoke, a removal, a role change) or a rebuild wakes it, and the dispatcher then looks again
on the worker thread. Nothing sleeps on a worker thread.
"""

from __future__ import annotations

import asyncio
import copy
import json
import logging
import secrets
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from . import frames
from .errors import HostError
from .journal import Batch, Cursors, Entry, Vis, Window, sees
from .transport import WRITE_QUEUE_BYTES, WRITE_QUEUE_FRAMES

if TYPE_CHECKING:
    from .service import Host
    from .transport import Session

__all__ = ["GRANT_TYPES", "PERSON_TYPES", "WAKE_TICKET", "WAKE_WORKSPACE", "Bus", "Feed", "Subscription", "Watch"]

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
        "device.excluded",
        "policy.changed",
        "settings.changed",
        "addon.granted",
        "addon.disabled",
        "addon.enabled",
        "addon.purged",
        "restore",
        "invalid.acknowledged",
    }
)
#: only to a person-class owner and to the grant's own person (the ``grant.list`` rule)
GRANT_TYPES = frozenset({"grant.issued", "grant.requested", "grant.revoked"})
#: ticket events that can answer a parked ``wait`` (``orch.ops.decisions.decision``) or end it (``not_found``)
WAKE_TICKET = frozenset(
    {
        "question.answered",
        "gate.approved",
        "gate.changes_requested",
        "verdict.given",
        "gate.invalidated",
        "visibility.changed",
        "restore",
    }
)
#: workspace events after which a parked ``wait``'s next look may be refused (its grant or person no longer holds)
WAKE_WORKSPACE = frozenset(
    {
        "grant.revoked",
        "member.removed",
        "role.changed",
        "device.revoked",
        "device.removed",
        "device.excluded",
        "restore",
    }
)

MAX_ITEMS = 256
MAX_BYTES = 256 * 1024
#: a ``detail`` subscription's queue limit (§0.9, v1.5): its items carry ``summary`` and ``data``
MAX_DETAIL_BYTES = 1024 * 1024
#: what the items of one ``evt`` may add up to: the §0.2 frame limit less the envelope (``sub``, ``cursor``)
FRAME_BUDGET = frames.MAX_FRAME - 8192
MAX_LAG_SECONDS = 30.0
EVT_ITEMS = 64
PER_CONNECTION = 8
PER_UID = 64
TICK_SECONDS = 0.1
REFRESH_SECONDS = 1.0


def _size(item: dict[str, Any]) -> int:
    """The bytes an item takes in an ``evt`` frame (plus its separator)."""
    return len(json.dumps(item, ensure_ascii=False, separators=(",", ":")).encode("utf-8")) + 1


class Feed(Window):
    """The bus's mirror of one workspace's journal window."""

    def __init__(self, workspace_id: str) -> None:
        super().__init__()
        self.workspace_id = workspace_id
        self.epoch = ""
        #: each ticket's visibility now (as of the last batch)
        self.vis: dict[str, Vis] = {}
        #: each ticket's title now (as of the last batch): ``title`` of a ``detail`` item is the title at delivery
        self.titles: dict[str, str] = {}


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
    #: ``sub.open {detail:true}`` (v1.5): event items are ``ActivityItem``s
    detail: bool = False
    #: the backlog counted so far while blocked: entries up to ``counted`` yield ``count`` items of ``size`` bytes
    counted: int = 0
    count: int = 0
    size: int = 0
    ended: bool = False

    @property
    def live(self) -> bool:
        return self.request.answered or self.request in self.session.answering


@dataclass(eq=False)
class Watch:
    """A parked ``wait``: the entries that may concern it since it was registered (``uid`` and ``"workspace"``)."""

    workspace_id: str
    event: asyncio.Event = field(default_factory=asyncio.Event)
    uid: str | None = None  # the ticket, once the first look fixed it (before that: every entry is kept)
    hits: list[Entry] = field(default_factory=list)
    reset: bool = False

    def relevant(self) -> bool:
        """Does anything since the last look justify another one?"""
        if self.reset:
            return True
        for entry in self.hits:
            if entry.log == "workspace":
                if entry.type in WAKE_WORKSPACE:
                    return True
            elif entry.log == self.uid and entry.type in WAKE_TICKET:
                return True
        return False

    def clear(self) -> None:
        self.hits.clear()
        self.reset = False
        self.event.clear()


class Bus:
    def __init__(self, host: Host, *, clock: Callable[[], float] = time.monotonic) -> None:
        self.host = host
        self.clock = clock
        self.feeds: dict[str, Feed] = {}
        self.subs: dict[str, Subscription] = {}
        self.watches: dict[str, set[Watch]] = {}
        self.max_items, self.max_bytes, self.max_lag = MAX_ITEMS, MAX_BYTES, MAX_LAG_SECONDS
        self.max_detail_bytes = MAX_DETAIL_BYTES
        #: test seam: while set, nothing is delivered (items queue; revalidation and overflow still apply)
        self.hold = False
        self._kicked = False
        self._ticker: asyncio.Task[None] | None = None
        self._refreshing: set[str] = set()
        self._refreshed: dict[str, float] = {}
        self._emptied: list[asyncio.Future[None]] = []

    @property
    def cursors(self) -> Cursors:
        return self.host.cursors

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
        """The slow path: a reader whose write queue drained, a lag deadline. Batches and answers kick directly."""
        while True:
            await asyncio.sleep(TICK_SECONDS)
            try:
                if any(not s.ended and s.pos < self._head(s) for s in self.subs.values()):
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

    def reap(self, session: Session) -> None:
        """A connection closed (its close hook): end its subscriptions and free their quota slots."""
        for sub in list(self.subs.values()):
            if sub.session is session:
                self.end(sub, "closed")

    async def emptied(self) -> None:
        """Wait until no subscription is left (a test hook)."""
        if not self.subs:
            return
        future = asyncio.get_running_loop().create_future()
        self._emptied.append(future)
        await future

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
        wid = batch.workspace_id
        feed = self.feeds.get(wid)
        if batch.reset:
            self.end_all("reset", wid)  # a rebuilt (or failed) journal: every subscription takes a snapshot
        if feed is None or batch.reset:
            feed = Feed(wid)
            self.feeds[wid] = feed
            feed.vis = dict(batch.vis)
            feed.titles = dict(batch.titles)
        else:
            feed.vis.update(batch.vis)
            feed.titles.update(batch.titles)
        feed.window.extend(batch.entries)
        feed.epoch, feed.head = batch.epoch, batch.head
        for watch in self.watches.get(wid, ()):
            if batch.reset:
                watch.reset = True
            else:
                watch.hits.extend(
                    e for e in batch.entries if watch.uid is None or e.log == watch.uid or e.log == "workspace"
                )
            if watch.relevant():
                watch.event.set()
        self.kick()

    # -- wait
    def watch(self, workspace_id: str) -> Watch:
        """Register a parked wait: it keeps the entries that may concern it until :meth:`unwatch`."""
        watch = Watch(workspace_id)
        self.watches.setdefault(workspace_id, set()).add(watch)
        return watch

    def unwatch(self, watch: Watch) -> None:
        watches = self.watches.get(watch.workspace_id)
        if watches is not None:
            watches.discard(watch)
            if not watches:
                del self.watches[watch.workspace_id]

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
        detail: bool = False,
    ) -> Subscription:
        """Register a subscription whose live boundary the worker fixed at ``pos`` (§0.11 quotas apply)."""
        if self.host.draining:
            raise self.host.restarting()
        if session.closed:
            raise HostError("cancelled", "the connection is closed")
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
            session.peer.uid, pos, counted=pos, detail=detail,
        )  # fmt: skip
        self.subs[sid] = sub
        session.subscriptions += 1
        if not mine:
            session.on_close.append(lambda: self.reap(session))
        return sub

    def close(self, session: Session, sid: str) -> bool:
        """``sub.close``: ``False`` for a subscription this connection does not hold (already ended included)."""
        sub = self.subs.get(sid)
        if sub is None or sub.session is not session:
            return False
        # after the res of sub.close (the caller answers first): one evt {end:"closed"}
        asyncio.get_running_loop().call_soon(self.end, sub, "closed")
        return True

    def cursor(self, sub: Subscription, jpos: int | None = None) -> str:
        feed = self.feeds.get(sub.workspace_id)
        jpos = sub.pos if jpos is None else jpos
        epoch, ref = (feed.epoch, feed.ref_at(jpos)) if feed is not None else ("", "")
        return self.cursors.encode(sub.workspace_id, sub.principal, epoch, jpos, ref)

    def end(self, sub: Subscription, reason: str) -> None:
        if sub.ended:
            return
        sub.ended = True
        self.subs.pop(sub.id, None)
        sub.session.subscriptions = max(0, sub.session.subscriptions - 1)
        if not self.subs:
            for future in self._emptied:
                if not future.done():
                    future.set_result(None)
            self._emptied.clear()
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
        fresh: dict[str, bool] = {}  # one freshness stat per workspace per flush
        for sub in list(self.subs.values()):
            if not sub.ended:
                self._flush(sub, fresh)

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
        if sub.column == "N":  # no principal to revalidate: it sees workspace items only
            return None
        return "person.removed"  # any other column (R included) is never served a subscription: end it (fail closed)

    def _flush(self, sub: Subscription, fresh: dict[str, bool]) -> None:
        session = sub.session
        if session.closed:
            self.end(sub, "closed")
            return
        if not sub.live:
            return
        wid = sub.workspace_id
        feed = self.feeds.get(wid)
        service = self._service(wid)
        if feed is None or service is None or sub.pos >= feed.head:
            return  # nothing to deliver: the next entry revalidates
        auth = service.auth
        if auth is None or auth.stamp is None or service.state == "unavailable" or service.journal is None:
            self.end(sub, "reset")  # nothing can be revalidated or delivered (no refresh spin): snapshot later
            return
        if wid not in fresh:
            fresh[wid] = auth.fresh(service.entry.store_dir)
        if not fresh[wid]:
            self._refresh(service)  # never deliver on a snapshot older than the log: the worker refreshes it
            return
        reason = self.principal_end(sub, auth, service.clock())
        if reason is not None:
            self.end(sub, reason)  # queued items are dropped with it
            return
        if sub.pos < feed.floor:
            self.end(sub, "reset")
            return
        owner = sub.column == "P" and auth.owner(sub.person)
        transport_full = not self._room(session)
        if self.hold or transport_full or self._write_in_flight(session):
            if transport_full and sub.blocked_since is None:
                sub.blocked_since = self.clock()
            if self._overflowing(sub, feed, owner):
                self.end(sub, "overflow")
            return
        sub.blocked_since = None
        self._deliver(sub, feed, feed.after(sub.pos), owner)
        sub.counted, sub.count, sub.size = sub.pos, 0, 0

    def _refresh(self, service: Any) -> None:
        wid = service.entry.workspace_id
        now = self.clock()
        if wid in self._refreshing or now - self._refreshed.get(wid, now - REFRESH_SECONDS) < REFRESH_SECONDS:
            return
        self._refreshing.add(wid)
        self._refreshed[wid] = now

        def done(_: Any) -> None:
            self._refreshing.discard(wid)
            self.kick()

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
                out.append(self._event_item(sub, feed, entry))
        elif "tickets" in sub.topics or entry.log in sub.tickets:
            before, after = sees(person, entry.before), sees(person, entry.after)
            if after and sees(person, feed.vis.get(entry.log)):
                item = self._event_item(sub, feed, entry)
                if not before and entry.before is not None and not sub.detail:
                    # the v1.4 item says why it appears; a detail item is the ActivityItem of the same id, unchanged
                    item["type"] = "visibility.changed"
                out.append(item)
            elif before and not after:  # hidden at this jpos: only the uid the viewer knew, never the event
                out.append({"id": f"{entry.jpos}.g", "jpos": entry.jpos, "uid": entry.log, "gone": True})
        if entry.needs and "needs" in sub.topics:
            out.append({"id": f"{entry.jpos}.n", "jpos": entry.jpos, "type": "needs.changed"})
        return out

    @staticmethod
    def _event_item(sub: Subscription, feed: Feed, entry: Entry) -> dict[str, Any]:
        """The event item: the v1.4 ``Item``, or with ``detail`` the ``ActivityItem`` of the same event, its ``title``
        the ticket's title now (§0.9)."""
        if not sub.detail or entry.detail is None:
            return entry.item()
        item = copy.deepcopy(dict(entry.detail))  # the journal's entry is shared by every subscription
        if entry.log != "workspace":
            item["title"] = feed.titles.get(entry.log)
        return item

    @staticmethod
    def _workspace_visible(sub: Subscription, entry: Entry, owner: bool) -> bool:
        """§0.9 (v1.5): ``activity.list``'s per-type rule, with the viewer's role now."""
        if sub.person is None:
            return False
        if entry.type in GRANT_TYPES:
            return owner or (entry.grant_person is not None and entry.grant_person == sub.person)
        return entry.type in PERSON_TYPES

    def _overflowing(self, sub: Subscription, feed: Feed, owner: bool) -> bool:
        """Count the backlog incrementally: only entries not counted yet are rendered (each once while blocked). The
        count is an estimate for the limit only: what is delivered is rendered again at delivery, for the viewer then,
        so a role change while blocked still counts (no position is passed here)."""
        if sub.blocked_since is not None and self.clock() - sub.blocked_since > self.max_lag:
            return True
        for entry in feed.after(max(sub.counted, sub.pos)):
            items = self.items(sub, feed, entry, owner)
            sub.counted = entry.jpos
            if not items:
                continue
            sub.count += len(items)
            sub.size += sum(len(json.dumps(item, separators=(",", ":"))) for item in items)
            if sub.count > self.max_items or sub.size > (self.max_detail_bytes if sub.detail else self.max_bytes):
                return True
        return False

    def _deliver(self, sub: Subscription, feed: Feed, pending: list[Entry], owner: bool) -> None:
        batch: list[dict[str, Any]] = []
        size = 0  # the bytes of ``batch`` (counted for detail items only: the v1.4 ones are tiny)
        upto = sub.pos
        for entry in pending:
            items = self.items(sub, feed, entry, owner)
            cost = sum(_size(i) for i in items) if sub.detail else 0
            if batch and (len(batch) + len(items) > EVT_ITEMS or size + cost > FRAME_BUDGET):
                if not self._send(sub, feed, batch, upto):
                    return
                batch, size = [], 0
            batch.extend(items)
            size += cost
            upto = entry.jpos
        if batch:
            self._send(sub, feed, batch, upto)
        else:
            sub.pos = upto  # filtered positions advance the cursor; the next evt carries it

    def _send(self, sub: Subscription, feed: Feed, items: list[dict[str, Any]], upto: int) -> bool:
        if not self._room(sub.session):
            return False
        frame = {"t": "evt", "sub": sub.id, "cursor": self.cursor(sub, upto), "items": items}
        try:
            sub.session.send(frame)
        except frames.FrameError as exc:
            sub.session.failure = exc
            return False
        sub.pos = upto
        return True
