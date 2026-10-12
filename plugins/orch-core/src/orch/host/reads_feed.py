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

**Paging.** The first page takes ``ctx.sub_cursor()`` first, then fixes the snapshot: the journal head at that moment,
the visibility of every ticket then, ``sub_cursor`` and ``as_of``, kept host-side (:mod:`orch.host.pages`). A later
page reads only events up to the snapshot's ``jpos``, filters them by the snapshot's visibility **and** the visibility
of its own request's viewer, and continues from the last position the previous page scanned. A request scans at most
:data:`SCAN_BUDGET` events: a page holds ``limit`` items or fewer when the budget ran out, and ``next_page`` is ``null``
only when no further visible item exists (a decision the budget may leave open, then a token continues the scan).
"""

from __future__ import annotations

from typing import Any

from . import readmodel
from .errors import HostError
from .journal import peek_cursor
from .reads import ReadCtx, handler

__all__ = ["SCAN_BUDGET"]

#: events one request may examine (§0.8: "at least 10 000"); a test lowers it
SCAN_BUDGET = 10_000
OP = "activity.list"


def _actor_matches(e: dict[str, Any], kinds: set[str] | None, person: str | None) -> bool:
    a = e.get("actor") if isinstance(e.get("actor"), dict) else {}
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

    events = store.merged_events()  # jpos = index + 1, the journal's numbering
    last = min(len(events), snap_jpos)
    tickets = store.state.tickets
    vis_now = {uid: viewer.sees(view) for uid, view in tickets.items()}
    if snap is None:
        snap_vis = dict(vis_now)
        data = {"jpos": snap_jpos, "vis": snap_vis, "sub_cursor": sub_cursor, "as_of": as_of}
        snap = ctx.pages.open(OP, ctx.principal, ctx.epoch, viewer.person, data)
        position = 0 if order == "asc" else last + 1
    ws = ctx.workspace

    kinds = set(args["actor_kind"]) if "actor_kind" in args else None
    types = set(args["types"]) if "types" in args else None
    person, since, until = args.get("person"), args.get("since"), args.get("until")

    def shown(uid: str) -> bool:
        return bool(snap_vis.get(uid)) and vis_now.get(uid, False)

    def passes(jpos: int) -> tuple[str, dict[str, Any], str | None] | None:
        """``(log, event, grant person)`` when the event matches every filter and the viewer may see it."""
        log, e = events[jpos - 1]
        typ = str(e.get("type"))
        if log == "workspace":
            if scope == "tickets" or wanted_uid is not None:
                return None
        elif scope == "workspace" or (wanted_uid is not None and log != wanted_uid) or not shown(log):
            return None
        if types is not None and typ not in types:
            return None
        if not _actor_matches(e, kinds, person):
            return None
        at = str(e.get("at"))
        if (since is not None and at < since) or (until is not None and at >= until):
            return None
        if log != "workspace":
            return log, e, None
        gp = _grant_person(typ, e, ws.grants)
        return (log, e, gp) if readmodel.ws_event_visible(typ, viewer.person, viewer.owner, gp) else None

    # the criteria and tasks before each ticket.updated of a visible ticket (what its summary names); nothing of a
    # ticket the viewer may not see is replayed
    summaries: dict[int, str] = {}
    state = readmodel.SummaryState()
    for jpos in range(1, last + 1):
        log, e = events[jpos - 1]
        if log != "workspace" and e.get("type") == "ticket.updated" and shown(log):
            if wanted_uid is None or log == wanted_uid:
                summaries[jpos] = readmodel.summary(e, state.of(log))

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
        log, e, gp = hit
        if log == "workspace":
            item = readmodel.activity_item(
                cursor, log, e, key=None, title=None, summary_text=readmodel.summary(e),
                data=readmodel.data_of(log, e, grant_person=gp, devices=ws.devices),
            )  # fmt: skip
        else:
            view = tickets[log]
            text = summaries.get(cursor)
            if text is None:
                text = readmodel.summary(e)
            item = readmodel.activity_item(
                cursor, log, e, key=view.key, title=view.title, summary_text=text, data=readmodel.data_of(log, e)
            )
        items.append(item)
        cursor += step
    token = ctx.pages.token(snap, cursor - step, args) if more else None  # resumes at ``cursor``
    return {"items": items, "next_page": token, "sub_cursor": sub_cursor, "as_of": as_of}
