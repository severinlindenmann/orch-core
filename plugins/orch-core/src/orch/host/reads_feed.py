"""H6b reads: ``activity.list`` (§0.8, cap ``activity/1``), the paged, filtered feed over the verified logs.

**What it lists.** ``store.merged_events()`` is the merged order of F1 §5.5 and an event's ``jpos`` is its 1-based index
in it, the same number the H6a journal gives it (so ``id`` ``"<jpos>.e"`` is the id of the subscription item of that
event and a client merges the live tail into a page by ``id``).

**Per item, before anything is computed (§0.8).** A ticket-log event is returned only when its ticket is visible to the
viewer at the snapshot **and** now (the ticket's visibility now, never when the event was appended). A workspace-log
event only by :func:`~orch.host.readmodel.ws_event_visible`; ``no-person`` gets none. ``key``, ``title``,
``summary`` and ``data`` are built only for an item that passed, from that event alone (a ``ticket.updated`` summary
needs the criteria and tasks before it, so those are replayed over the **visible** tickets' ``ticket.updated`` events
only).

**Cost.** The viewer-independent part of an event (its ``ActivityItem`` with ``key`` and ``title`` left out, its
grant person) is derived once per workspace and kept in :class:`Base`, keyed by (journal epoch, journal build): the
**newest** :data:`MAX_ENTRIES` events and at most :data:`MAX_BYTES` (estimated), extended from the journal window
(``Entry.detail``) and rebuilt from the verified logs on an epoch change or when the window no longer reaches the cached
head; a cache unused for :data:`IDLE_SECONDS` is dropped. A position older than the cache (an ``order:"asc"`` scan or a
deep page) is derived on demand from the logs for the range the request can still scan within :data:`SCAN_BUDGET`, and
dropped after the request. Nothing viewer-specific is cached: visibility, ``key`` and ``title`` are looked up per
request, and only for tickets that pass. Items handed out are copies: no cached object is shared with a response.

**Paging.** The first page takes ``ctx.sub_cursor()`` first, then fixes the snapshot: the journal head at that moment,
the tickets **not** visible then (or, if fewer, the visible ones: :func:`pack_vis`), ``sub_cursor`` and ``as_of``,
kept host-side (:mod:`orch.host.pages`) only when there is a next page. A later
page reads only events up to the snapshot's ``jpos``, filters them by the snapshot's visibility **and** the visibility
of its own request's viewer, and continues from the last position the previous page scanned. A request scans at most
:data:`SCAN_BUDGET` events: a page holds ``limit`` items or fewer when the budget ran out, and ``next_page`` is ``null``
only when no further visible item exists (a decision the budget may leave open, then a token continues the scan).
"""

from __future__ import annotations

import copy
import json
import threading
import time
import weakref
from collections.abc import Iterable
from typing import Any, NamedTuple

from . import readmodel
from .errors import HostError
from .journal import grant_person_of, peek_cursor
from .reads import ReadCtx, handler

__all__ = ["IDLE_SECONDS", "MAX_BYTES", "MAX_ENTRIES", "SCAN_BUDGET", "pack_vis", "unpack_vis"]

#: events one request may examine (§0.8: "at least 10 000"); a test lowers it
SCAN_BUDGET = 10_000
#: the base cache keeps the newest entries only: at most this many, and at most this many (estimated) bytes
MAX_ENTRIES = 50_000
MAX_BYTES = 64 * 1024 * 1024
#: cold chunks kept (within :data:`MAX_BYTES`)
COLD_CHUNKS = 4
#: principals deriving a cold chunk now (host-wide): at most one cold derivation per principal at a time
_COLD_BUSY: set[str] = set()
_COLD_LOCK = threading.Lock()
#: a cache not used for this long (seconds) is dropped
IDLE_SECONDS = 600
SWEEP_SECONDS = 60
OP = "activity.list"


class Entry(NamedTuple):
    """The viewer-independent derivation of one event (cached)."""

    log: str
    typ: str
    at: str
    actor: dict[str, Any]
    grant_person: str | None
    detail: dict[str, Any]  # the ActivityItem with key/title None
    size: int = 0  # estimated bytes held


def _size(detail: dict[str, Any]) -> int:
    return 400 + len(detail["summary"]) + len(json.dumps(detail["data"], default=str, separators=(",", ":")))


class Base:
    """Per-workspace cache of the newest :class:`Entry` s (jpos ``first`` .. ``first + len - 1``), valid for one journal
    epoch **and** one journal build: a rebuild (a foreign append, which may shift positions without a new epoch, or a
    restore) drops it. Entries are viewer-independent; visibility, key and title are applied per request. Capped at
    :data:`MAX_ENTRIES` / :data:`MAX_BYTES` (the oldest go first) and dropped when idle (:func:`sweep`)."""

    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.key: tuple[str, int] = ("", -1)
        self.first = 1
        self.entries: list[Entry] = []
        self.bytes = 0
        self.used = time.monotonic()
        #: cold chunks older than the cache, newest use last: ``(lo, entries, summary state after the chunk)``; they
        #: count toward :data:`MAX_BYTES`, so repeated deep or ``asc`` pages do not rescan the logs
        self.cold: list[tuple[int, list[Entry], Any]] = []
        self.cold_bytes = 0
        _CACHES.add(self)

    def clear(self) -> None:
        with self.lock:
            self.key, self.first, self.entries, self.bytes = ("", -1), 1, [], 0
            self.cold, self.cold_bytes = [], 0

    def cold_at(self, jpos: int, key: tuple[str, int]) -> Window | None:
        """A kept cold chunk holding ``jpos``, only while the cache is still of ``key`` (epoch, journal build): checked
        at read time, under the lock that builds and invalidates it."""
        if self.key != key:
            return None
        for i, (lo, entries, _state) in enumerate(self.cold):
            if lo <= jpos < lo + len(entries):
                self.cold.append(self.cold.pop(i))  # most recently used last
                return Window(lo, entries)
        return None

    def cold_state(self, jpos: int, key: tuple[str, int]) -> Any:
        """The summary state after the chunk ending right before ``jpos`` (a continuation skips re-summarising)."""
        if self.key != key:
            return None
        for lo, entries, state in self.cold:
            if lo + len(entries) == jpos:
                return state
        return None

    def keep_cold(self, key: tuple[str, int], lo: int, entries: list[Entry], state: Any) -> None:
        """Publish a derived chunk only into the generation it was derived under: a rebuild or restore that ran
        meanwhile changed the key (or cleared the cache), and the chunk is dropped."""
        with self.lock:
            if self.key != key:
                return
            self.cold.append((lo, entries, state))
            self.cold_bytes += sum(en.size for en in entries)
            while self.cold and (len(self.cold) > COLD_CHUNKS or self.bytes + self.cold_bytes > MAX_BYTES):
                _lo, old, _st = self.cold.pop(0)
                self.cold_bytes -= sum(en.size for en in old)

    @property
    def end(self) -> int:
        """The jpos after the last cached entry."""
        return self.first + len(self.entries)

    def add(self, fresh: Iterable[Entry]) -> None:
        with self.lock:
            for en in fresh:
                self.entries.append(en)
                self.bytes += en.size
            self.trim()

    def trim(self) -> None:
        """Drop the oldest entries beyond the caps (in chunks, so the list is not shifted on every append)."""
        over, size = max(0, len(self.entries) - MAX_ENTRIES), self.bytes
        n = 0
        while n < len(self.entries) and (size > MAX_BYTES or n < over):
            size -= self.entries[n].size
            n += 1
        if n:
            n = min(len(self.entries), n + max(1, MAX_ENTRIES // 16))  # a slack chunk, amortised
            self.bytes -= sum(en.size for en in self.entries[:n])
            del self.entries[:n]
            self.first += n


#: every live cache, for the idle sweep
_CACHES: weakref.WeakSet[Base] = weakref.WeakSet()
_sweeper: threading.Thread | None = None


def sweep(now: float | None = None) -> int:
    """Drop every cache unused for :data:`IDLE_SECONDS`; returns how many were dropped."""
    now = time.monotonic() if now is None else now
    n = 0
    for cache in list(_CACHES):
        if cache.entries and now - cache.used > IDLE_SECONDS:
            cache.clear()
            n += 1
    return n


def _sweep_loop() -> None:
    while True:
        time.sleep(SWEEP_SECONDS)
        sweep()


def _start_sweeper() -> None:
    global _sweeper
    if _sweeper is None:
        _sweeper = threading.Thread(target=_sweep_loop, name="activity-base-sweep", daemon=True)
        _sweeper.start()


class Window(NamedTuple):
    """What a request reads of the cache: a stable view (``first``, the list it was cut from)."""

    first: int
    entries: list[Entry]

    @property
    def last(self) -> int:
        """The last jpos held (``first - 1`` when empty)."""
        return self.first + len(self.entries) - 1

    def at(self, jpos: int) -> Entry | None:
        i = jpos - self.first
        return self.entries[i] if 0 <= i < len(self.entries) else None


def _same(cached: Entry, entry: Any) -> bool:
    """A cached entry still names the journal's event at its position."""
    return cached.log == entry.log and cached.typ == entry.type and cached.at == entry.at


def _from_journal(entry: Any) -> Entry | None:
    d = entry.detail
    if d is None:
        return None
    detail = copy.deepcopy(dict(d))  # the journal's object is shared with the subscriptions: never hold it
    detail["key"] = detail["title"] = None
    return Entry(entry.log, entry.type, entry.at, detail["actor"], entry.grant_person, detail, _size(detail))


def derive(
    ctx: ReadCtx, lo: int, hi: int | None = None, *, keep: int | None = None, state: Any = None, out_state: Any = None
) -> tuple[int, list[Entry]]:
    """``(total, entries)`` from the verified logs: the entries of jpos ``lo..hi`` (all from ``lo`` when ``hi`` is
    ``None``), only the newest ``keep`` of them when set. Every event is summarised in order (the summary state needs
    them), but items are built for the range only (the expensive path: reads every log)."""
    ws = ctx.workspace
    resumed = state is not None  # the summary state right before ``lo``: earlier events need no summary
    state = copy.deepcopy(state) if resumed else readmodel.SummaryState()
    out: list[Entry] = []
    jpos = 0
    for jpos, (log, e) in enumerate(ctx.store.merged_events(), 1):
        if hi is not None and jpos > hi:
            jpos -= 1
            break
        if resumed and jpos < lo:
            continue
        typ = str(e.get("type"))
        text = readmodel.summary(e, state.of(log)) if log != "workspace" else readmodel.summary(e)
        if jpos < lo:
            continue
        gp = grant_person_of(typ, e, ws.grants) if log == "workspace" else None
        if log == "workspace":
            data = readmodel.data_of(log, e, grant_person=gp, devices=ws.devices)
        else:
            data = readmodel.data_of(log, e)
        item = readmodel.activity_item(jpos, log, e, key=None, title=None, summary_text=text, data=data, ws=ws)
        out.append(Entry(log, typ, str(e.get("at")), item["actor"], gp, item, _size(item)))
        if keep is not None and len(out) > 2 * keep:
            del out[: len(out) - keep]
    if keep is not None and len(out) > keep:
        del out[: len(out) - keep]
    if out_state is not None:
        out_state.append(state)
    return jpos, out


def base_entries(ctx: ReadCtx, upto: int) -> Window:
    """The cached window, reaching at least position ``upto`` when the logs have that many (jpos = first + index)."""
    service = ctx.service
    cache = getattr(service, "activity_base", None)
    if cache is None:
        cache = service.activity_base = Base()
        _start_sweeper()
    journal = service.journal
    key = (ctx.epoch, journal.generation if journal is not None else -1)
    with cache.lock:
        cache.used = time.monotonic()
        if cache.key != key:
            cache.clear()
            cache.key = key
        have = cache.end - 1  # the last jpos held
        if journal is not None and cache.entries:  # integrity: the last cached position still holds the same event
            at = journal.entry_at(min(have, journal.head))
            if have > journal.head or (at is not None and not _same(cache.entries[at.jpos - cache.first], at)):
                cache.clear()
                cache.key = key
                have = 0
        if have < upto or not cache.entries:
            fresh = None
            if journal is not None and journal.window and journal.floor <= have + 1:
                fresh = [_from_journal(x) for x in journal.after(have)]
            if fresh is not None and all(x is not None for x in fresh):
                cache.add(x for x in fresh if x is not None)
            else:
                cache.clear()
                cache.key = key
                total, entries = derive(ctx, 0, keep=MAX_ENTRIES)
                cache.first = total - len(entries) + 1
                cache.add(entries)
        return Window(cache.first, cache.entries)


def cold_window(ctx: ReadCtx, jpos: int, ahead: int, order_desc: bool, below: int) -> Window:
    """Entries older than the cache (positions ``< below``) for a scan at ``jpos`` that may still examine ``ahead``
    events, read from the logs for that range only and not kept."""
    cache = ctx.service.activity_base
    journal = ctx.service.journal
    key = (ctx.epoch, journal.generation if journal is not None else -1)  # the generation this request reads
    with cache.lock:
        hit = cache.cold_at(jpos, key)
        if hit is not None:
            return hit
    if order_desc:
        lo, hi = max(1, jpos - ahead + 1), jpos
    else:
        lo, hi = jpos, min(below - 1, jpos + ahead - 1)
    who = ctx.principal
    with _COLD_LOCK:
        if who in _COLD_BUSY:
            raise HostError("quota.host", "another deep activity.list read of yours is running: retry shortly")
        _COLD_BUSY.add(who)
    try:
        after: list[Any] = []
        with cache.lock:
            state = cache.cold_state(lo, key)
        _, entries = derive(ctx, lo, hi, state=state, out_state=after)
    finally:
        with _COLD_LOCK:
            _COLD_BUSY.discard(who)
    if journal is not None and (ctx.epoch, journal.generation) != key:  # rebuilt while deriving: serve nothing stale
        raise HostError("page.expired", "the history was rebuilt during the read: restart from the first page")
    cache.keep_cold(key, lo, entries, after[0] if after else None)
    return Window(lo, entries)


def _actor_matches(a: dict[str, Any], kinds: set[str] | None, person: str | None) -> bool:
    if kinds is not None and a.get("kind") not in kinds:
        return False
    return person is None or a.get("id") == person or a.get("for") == person


def pack_vis(all_uids: Iterable[str], seen: Iterable[str]) -> dict[str, list[str]]:
    """The snapshot's visibility, the smaller of two lists: ``{"hidden": [...]}`` (every ticket but these was visible)
    or ``{"shown": [...]}``. At most half of the tickets are listed, and a normal workspace lists only the few
    restricted ones: the snapshot stays far under the page-byte cap whatever the ticket count."""
    seen_set = set(seen)
    hidden = sorted(u for u in all_uids if u not in seen_set)
    shown = sorted(seen_set)
    return {"hidden": hidden} if len(hidden) <= len(shown) else {"shown": shown}


def unpack_vis(packed: dict[str, list[str]]) -> Any:
    """``uid -> bool`` for a log's uid, from :func:`pack_vis` (a ticket created after the snapshot has no event
    before it, so its answer is never asked)."""
    if "shown" in packed:
        shown = set(packed["shown"])
        return lambda uid: uid in shown
    hidden = set(packed["hidden"])
    return lambda uid: uid not in hidden


def _uid_of(ctx: ReadCtx, ref: str) -> str:
    """The uid ``ref`` names, or ``""`` (matches nothing) for a ticket gone or not visible to the viewer now."""
    try:
        return ctx.ticket(ref).uid
    except HostError:
        return ""


@handler(OP)
def activity_list(ctx: ReadCtx) -> dict[str, Any]:
    args = ctx.args
    viewer, store = ctx.viewer, ctx.store
    limit, order, scope = int(args.get("limit", 50)), args.get("order", "desc"), args.get("scope", "all")

    if "page" in args:
        snap, position = ctx.pages.resume(OP, args["page"], ctx.principal, ctx.epoch, viewer.person, args)
        snap_jpos, snap_vis = int(snap.data["jpos"]), unpack_vis(snap.data["vis"])
        sub_cursor, as_of, position = snap.data["sub_cursor"], snap.data["as_of"], int(position)
        # the first page named this ticket and the viewer could see it; one it cannot see now just matches nothing
        wanted_uid = _uid_of(ctx, args["ticket"]) if "ticket" in args else None
    else:
        if scope == "workspace" and "ticket" in args:
            raise HostError("invalid.input", "activity.list: ticket cannot be combined with scope workspace")
        if "since" in args and "until" in args and args["since"] > args["until"]:
            raise HostError("invalid.input", "activity.list: since is after until")
        wanted_uid = ctx.ticket(args["ticket"]).uid if "ticket" in args else None
        sub_cursor = ctx.sub_cursor()  # first: every event the snapshot shows is at or before this position
        body = peek_cursor(sub_cursor)
        snap_jpos = int(body["j"]) if body is not None else 0
        as_of, snap_vis, snap, position = ctx.as_of(), None, None, None

    store.load_all()  # every ticket's state (the visibility below); merged_events() used to do it
    events = base_entries(ctx, snap_jpos)  # the journal's numbering; older positions are derived on a miss
    last = min(events.last, snap_jpos)
    tickets = store.state.tickets
    vis_now = {uid: viewer.sees(view) for uid, view in tickets.items()}
    if snap is None:
        seen_now = {uid for uid, ok in vis_now.items() if ok}
        snap_vis = seen_now.__contains__
        data = {"jpos": snap_jpos, "vis": pack_vis(vis_now, seen_now), "sub_cursor": sub_cursor, "as_of": as_of}
        position = 0 if order == "asc" else last + 1

    kinds = set(args["actor_kind"]) if "actor_kind" in args else None
    types = set(args["types"]) if "types" in args else None
    person, since, until = args.get("person"), args.get("since"), args.get("until")

    def shown(uid: str) -> bool:
        return snap_vis(uid) and vis_now.get(uid, False)

    cold: Window | None = None

    def passes(jpos: int) -> Entry | None:
        """The entry when the event matches every filter and the viewer may see it."""
        nonlocal cold
        en = events.at(jpos)
        if en is None:  # older than the cache: derived from the logs for the range this request can still scan
            if cold is None or cold.at(jpos) is None:
                cold = cold_window(ctx, jpos, SCAN_BUDGET - scanned + 1, step < 0, events.first)
            en = cold.at(jpos)
            if en is None:
                return None
        log, typ = en.log, en.typ
        if log == "workspace":
            if scope == "tickets" or wanted_uid is not None:
                return None
        elif scope == "workspace" or (wanted_uid is not None and log != wanted_uid) or not shown(log):
            return None
        if types is not None and typ not in types:
            return None
        if not _actor_matches(en.actor, kinds, person):
            return None
        if (since is not None and en.at < since) or (until is not None and en.at >= until):
            return None
        if log != "workspace":
            return en
        return en if readmodel.ws_event_visible(typ, viewer.person, viewer.owner, en.grant_person) else None

    step = -1 if order == "desc" else 1
    items: list[dict[str, Any]] = []
    scanned = 0
    cursor = position + step  # the next jpos to examine
    more = False
    while 1 <= cursor <= last:
        if scanned >= SCAN_BUDGET:
            more = True  # undecided: a token continues the scan
            break
        scanned += 1
        hit = passes(cursor)
        if hit is None:
            cursor += step
            continue
        if len(items) == limit:  # a visible item beyond the page: there is a next page
            more = True
            break
        item = copy.deepcopy(hit.detail)  # a response never shares state with the cache
        if hit.log != "workspace":  # key and title now, only for a ticket that passed
            view = tickets[hit.log]
            item["key"], item["title"] = view.key, view.title
        items.append(item)
        cursor += step
    if more and snap is None:  # a snapshot only when a later page can follow
        snap = ctx.pages.open(OP, ctx.principal, ctx.epoch, viewer.person, data)
    token = ctx.pages.token(snap, cursor - step, args) if more else None  # resumes at ``cursor``
    return {"items": items, "next_page": token, "sub_cursor": sub_cursor, "as_of": as_of}
