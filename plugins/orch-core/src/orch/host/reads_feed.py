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

**Cost.** The viewer-independent part of every event (its ``ActivityItem`` with ``key`` and ``title`` left out, its
grant person) is derived once per workspace and kept in :class:`Base`, keyed by (journal epoch, journal head): it is
extended from the journal window (``Entry.detail``) when the epoch is unchanged and rebuilt from the verified logs
on an epoch change or when the window no longer reaches the cached head. Nothing viewer-specific is cached: visibility,
``key`` and ``title`` are looked up per request, and only for tickets that pass.

**Paging.** The first page takes ``ctx.sub_cursor()`` first, then fixes the snapshot: the journal head at that moment,
the visibility of every ticket then, ``sub_cursor`` and ``as_of``, kept host-side (:mod:`orch.host.pages`) only when
there is a next page. A later
page reads only events up to the snapshot's ``jpos``, filters them by the snapshot's visibility **and** the visibility
of its own request's viewer, and continues from the last position the previous page scanned. A request scans at most
:data:`SCAN_BUDGET` events: a page holds ``limit`` items or fewer when the budget ran out, and ``next_page`` is ``null``
only when no further visible item exists (a decision the budget may leave open, then a token continues the scan).
"""

from __future__ import annotations

from typing import Any, NamedTuple

from . import readmodel
from .errors import HostError
from .journal import peek_cursor
from .reads import ReadCtx, handler

__all__ = ["SCAN_BUDGET"]

#: events one request may examine (§0.8: "at least 10 000"); a test lowers it
SCAN_BUDGET = 10_000
OP = "activity.list"


class Entry(NamedTuple):
    """The viewer-independent derivation of one event (cached)."""

    log: str
    typ: str
    at: str
    actor: dict[str, Any]
    grant_person: str | None
    detail: dict[str, Any]  # the ActivityItem with key/title None


class Base:
    """Per-workspace cache of :class:`Entry` by jpos, valid for one journal epoch."""

    def __init__(self) -> None:
        self.epoch = ""
        self.entries: list[Entry] = []


def _from_journal(entry: Any) -> Entry | None:
    d = entry.detail
    if d is None:
        return None
    return Entry(entry.log, entry.type, entry.at, d["actor"], entry.grant_person, dict(d))


def _from_events(ctx: ReadCtx) -> list[Entry]:
    """Every event of the verified logs, derived once (the expensive path: reads every log)."""
    ws = ctx.workspace
    state = readmodel.SummaryState()
    out: list[Entry] = []
    for jpos, (log, e) in enumerate(ctx.store.merged_events(), 1):
        typ = str(e.get("type"))
        gp = _grant_person(typ, e, ws.grants) if log == "workspace" else None
        if log == "workspace":
            text, data = readmodel.summary(e), readmodel.data_of(log, e, grant_person=gp, devices=ws.devices)
        else:
            text, data = readmodel.summary(e, state.of(log)), readmodel.data_of(log, e)
        item = readmodel.activity_item(jpos, log, e, key=None, title=None, summary_text=text, data=data)
        out.append(Entry(log, typ, str(e.get("at")), item["actor"], gp, item))
    return out


def base_entries(ctx: ReadCtx, upto: int) -> list[Entry]:
    """The cached entries, at least ``upto`` of them when the logs have that many (jpos = index + 1)."""
    service = ctx.service
    cache = getattr(service, "activity_base", None)
    if cache is None:
        cache = service.activity_base = Base()
    journal = service.journal
    if cache.epoch != ctx.epoch:
        cache.epoch, cache.entries = ctx.epoch, []
    have = len(cache.entries)
    if have >= upto:
        return cache.entries
    if journal is not None and journal.window and journal.floor <= have + 1:
        fresh = [_from_journal(x) for x in journal.after(have)]
        if all(x is not None for x in fresh):
            cache.entries.extend(x for x in fresh if x is not None)
            if len(cache.entries) >= upto:
                return cache.entries
    cache.entries = _from_events(ctx)
    return cache.entries


def _actor_matches(a: dict[str, Any], kinds: set[str] | None, person: str | None) -> bool:
    if kinds is not None and a.get("kind") not in kinds:
        return False
    return person is None or a.get("id") == person or a.get("for") == person


def _grant_person(typ: str, e: dict[str, Any], grants: Any) -> str | None:
    """The person of the grant a ``grant.*`` event names: a ``grant.issued``'s signer, a ``grant.revoked``'s grant."""
    if typ == "grant.issued":
        a = e.get("actor") if isinstance(e.get("actor"), dict) else {}
        return a.get("id") if isinstance(a.get("id"), str) else None
    if typ == "grant.revoked":
        g = grants.get(e.get("grant"))
        return getattr(g, "person", None) if g is not None else None
    return None


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
        snap_jpos, snap_vis = int(snap.data["jpos"]), snap.data["vis"]
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
    events = base_entries(ctx, snap_jpos)  # jpos = index + 1, the journal's numbering
    last = min(len(events), snap_jpos)
    tickets = store.state.tickets
    vis_now = {uid: viewer.sees(view) for uid, view in tickets.items()}
    if snap is None:
        snap_vis = dict(vis_now)
        data = {"jpos": snap_jpos, "vis": snap_vis, "sub_cursor": sub_cursor, "as_of": as_of}
        position = 0 if order == "asc" else last + 1

    kinds = set(args["actor_kind"]) if "actor_kind" in args else None
    types = set(args["types"]) if "types" in args else None
    person, since, until = args.get("person"), args.get("since"), args.get("until")

    def shown(uid: str) -> bool:
        return bool(snap_vis.get(uid)) and vis_now.get(uid, False)

    def passes(jpos: int) -> Entry | None:
        """The entry when the event matches every filter and the viewer may see it."""
        en = events[jpos - 1]
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
        item = dict(hit.detail)
        if hit.log != "workspace":  # key and title now, only for a ticket that passed
            view = tickets[hit.log]
            item["key"], item["title"] = view.key, view.title
        items.append(item)
        cursor += step
    if more and snap is None:  # a snapshot only when a later page can follow
        snap = ctx.pages.open(OP, ctx.principal, ctx.epoch, viewer.person, data)
    token = ctx.pages.token(snap, cursor - step, args) if more else None  # resumes at ``cursor``
    return {"items": items, "next_page": token, "sub_cursor": sub_cursor, "as_of": as_of}
