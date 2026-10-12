"""H6b reads: ``ticket.query`` and ``needs`` (§0.8, §0.8.4, v1.4 ``read_ext/1`` and v1.5 ``read_ext/2`` fields).

Both are snapshot reads: they take ``ctx.sub_cursor()`` right before they read the state, so ``sub.open {after}``
with it loses and repeats nothing.

**Needs.** The base is :mod:`orch.model.needs` (``TicketView.needs``, ``WorkspaceView.needs``). The model produces no
``addon`` need today (``orch.addons.needs`` is not wired into the replay), so an ``addon`` item appears only if a
``Need`` of that kind is ever in a view; its conversion is here. :func:`need_item` applies §0.8's limits: ``who`` is
cut to persons who can see the ticket (``orch.model.visibility.can_see``), an ``approve`` nobody visible can decide
becomes ``no_eligible`` with ``cause:"no_visible_person"``, a model ``no_eligible`` has ``cause:"no_token"``, and a
``question`` whose addressees all drop out falls back to the owners and maintainers who can see the ticket.
``GateView.blocked`` is not touched.

**Rows** are computed for tickets the viewer may see, and each derived value (``claim``, ``tasks``, ``criteria``,
``blocking_questions``, ``closed``, ``restricted``, ``epic``) only for a ticket that passed the filter, from that
ticket's own log; ``epic`` is walked through tickets the viewer may see only (§0.5 "Computed after the filter").

**Paging.** A first page with a next page stores the filtered, sorted **uids** (a compact list) in a host-side
snapshot (``ctx.pages``): it fixes membership and order (§0.8). Every snapshot row was visible to the first viewer, and
a later page has the same person (:meth:`~orch.host.pages.Pages.resume`), so the snapshot visibility needs no storage.
A later page rebuilds each row it serves from the **current** view, for its own request's viewer, keeping a uid only
if its ticket is visible now. ``total`` counts the snapshot's uids that pass that filter; ``next_page`` is ``null``
only when no further visible row exists. Cost: O(rows in the snapshot) per later page (one visibility check each, to
count ``total`` and find the next row); only the ``limit`` rows served are rebuilt.
"""

from __future__ import annotations

import unicodedata
from collections.abc import Mapping
from typing import Any

from orch.model.needs import Need
from orch.store import StoreError
from orch.store.render import thaw

from .errors import HostError
from .readmodel import put_snip
from .reads import ReadCtx, handler

__all__ = ["need_item", "ticket_items"]

KIND_ORDER = ("question", "approve", "no_eligible", "acknowledge", "addon")
PRIORITY_RANK = {"urgent": 0, "high": 1, "medium": 2, "low": 3}
EPIC_LEVELS = 8
MANAGER_ROLES = ("owner", "maintainer")


# --- who can see what ----------------------------------------------------------------------------------------------


def can_see(ws: Any, view: Any, person: str) -> bool:
    """``orch.model.visibility.can_see`` on views: a member, and on the list if the ticket is restricted."""
    if person not in ws.members:
        return False
    vis = view.visibility
    return vis == "workspace" or person in vis["restricted"]


def managers(ws: Any, view: Any) -> list[str]:
    """Owners and maintainers who can see the ticket (F1 §5.4.2)."""
    return sorted(p for p, m in ws.members.items() if m.role in MANAGER_ROLES and can_see(ws, view, p))


def _fixers(ws: Any, view: Any) -> list[str]:
    """Who can fix a gate: the ticket owner and the workspace owners and maintainers, all able to see the ticket."""
    out = set(managers(ws, view))
    if can_see(ws, view, view.owner):
        out.add(view.owner)
    return sorted(out)


# --- needs ---------------------------------------------------------------------------------------------------------


def _question_since(events: list[dict[str, Any]]) -> dict[str, str]:
    """``{question id: at}`` of the ``question.asked`` that defines the current text: a re-ask with the same hash does
    not restart it, a changed hash does."""
    out: dict[str, str] = {}
    hashes: dict[str, str] = {}
    for e in events:
        if e.get("type") != "question.asked":
            continue
        qid = e["question"]["id"]
        if hashes.get(qid) != e.get("hash"):
            hashes[qid] = e.get("hash")
            out[qid] = e["at"]
    return out


def need_item(
    ctx: ReadCtx, view: Any, need: Need, *, full: bool = True, since: Mapping[Any, str] | None = None
) -> dict[str, Any]:
    """The ``NeedItem`` of one model need of ``view`` with ``who`` limited (§0.8). ``full`` adds ``since``, ``text``,
    ``status`` and ``payload``; ``since`` is ``{(model kind, ref): at}`` (the journal's, and the questions')."""
    ws = ctx.workspace
    kind, ref = need.kind, need.ref
    who = sorted({p for p in need.who if can_see(ws, view, p)})
    payload: dict[str, Any] = {}
    text: str | None = None
    if kind == "question":
        if not who:
            who = managers(ws, view)
        q = next((x for x in thaw(view.fields["questions"]) if x["id"] == ref), None)
        if full and q is not None:
            text = q["text"]
            payload = {
                "question": ref,
                "blocking": bool(q["blocking"]),
                "to": q["to"],
                "options": [dict(o) for o in q.get("options") or []],
                **{k: q[k] for k in ("recommended", "why") if q.get(k) is not None},
            }
    elif kind == "approve":
        gate = view.gates[ref]
        if who:
            payload = {"gate": ref, "gen": gate.gen, "count": gate.needed, "have": len(gate.counting)}
        else:
            kind, who = "no_eligible", _fixers(ws, view)
            payload = {"gate": ref, "cause": "no_visible_person"}
    elif kind == "no_eligible":
        who = _fixers(ws, view)
        payload = {"gate": ref, "cause": "no_token"}
    elif kind == "acknowledge":
        payload = {"scope": "ticket"}
    elif kind == "addon":
        addon, _, rule = (ref or "").partition(".")
        payload = {"addon": addon, "rule": rule}
        text = need.detail
    item: dict[str, Any] = {
        "kind": kind,
        "key": view.key,
        "uid": view.uid,
        "ref": ref,
        "detail": "" if need.kind == "addon" else need.detail,
        "who": who,
    }
    if full:
        at = (since or {}).get((need.kind, ref))
        if at is not None and need.kind != "acknowledge":
            item["since"] = at
        if text is not None:
            put_snip(item, "text", text)
        item["status"] = view.status
        item["payload"] = payload
    return item


def ticket_items(ctx: ReadCtx, view: Any, *, full: bool = True) -> list[dict[str, Any]]:
    """Every need of ``view`` as ``needs`` reports it (before any person filter), in the model's order."""
    if not view.needs:
        return []
    since: dict[tuple[str, str | None], str] = {}
    if full:
        journal = ctx.service.journal
        if journal is not None:
            since.update(journal.need_since(view.uid))
        if any(n.kind == "question" for n in view.needs):
            for qid, at in _question_since(ctx.store.events(view.uid)).items():
                since[("question", qid)] = at
    return [need_item(ctx, view, n, full=full, since=since) for n in view.needs]


def _workspace_items(ctx: ReadCtx) -> list[dict[str, Any]]:
    return [
        {
            "kind": n.kind,
            "key": None,
            "uid": None,
            "ref": n.ref,
            "detail": n.detail,
            "who": sorted(n.who),
            "payload": {"scope": "workspace"},
        }
        for n in ctx.workspace.needs
    ]


@handler("needs")
def needs(ctx: ReadCtx) -> dict[str, Any]:
    """``needs {person?}`` (§0.8, §0.5): the items of ``person`` (the viewer by default), on tickets the caller's viewer
    sees and, with a person, that person sees too. Column N (no person) gets every item on ``workspace`` tickets and
    no workspace-level item (§0.5 says "workspace items only"; the contract leaves ``who`` of those open, so none is
    filtered by it)."""
    viewer = ctx.viewer
    person = viewer.needs_person(ctx.args.get("person"))
    cursor = ctx.sub_cursor()
    store, ws = ctx.store, ctx.workspace
    store.load_all()
    items: list[dict[str, Any]] = []
    for view in sorted(store.state.tickets.values(), key=lambda v: _key_order(v.key)):
        if not view.needs or not viewer.sees(view):
            continue
        if person is not None and not can_see(ws, view, person):
            continue
        for item in ticket_items(ctx, view):
            if person is None or person in item["who"]:
                items.append(item)
    if person is not None and viewer.person is not None:
        items.extend(
            i for i in _workspace_items(ctx) if person in i["who"] and viewer.person in i["who"]
        )  # only to a viewer in its who
    return {"items": items, "sub_cursor": cursor}


# --- ticket.query --------------------------------------------------------------------------------------------------


def _key_order(key: str) -> tuple[str, int]:
    prefix, _, number = key.rpartition("-")
    return prefix, int(number) if number.isdigit() else 0


def _fold(text: str) -> str:
    return unicodedata.normalize("NFC", text).casefold()


def _last_event(ctx: ReadCtx, uid: str) -> dict[str, Any]:
    """``at`` and ``seq`` of the ticket's last event: from the journal (O(1), no store lock per ticket), else the
    store (a stale journal, or a ticket it does not know)."""
    journal = ctx.service.journal
    last = journal.last_event(uid) if journal is not None else None
    if last is not None:
        return {"at": last[0], "seq": last[1]}
    head = ctx.store.head_seq(uid)
    evs = ctx.store.events(uid, after=head - 1)
    return evs[-1] if evs else {"at": "", "seq": head}


def _by_key(ctx: ReadCtx) -> dict[str, Any]:
    """Every loaded ticket view by key, built once per request (no store lock per parent lookup)."""
    cached = getattr(ctx, "_by_key_cache", None)
    if cached is None:
        cached = {v.key: v for v in ctx.store.state.tickets.values()}
        ctx._by_key_cache = cached  # type: ignore[attr-defined]
    return cached


def _epic(ctx: ReadCtx, view: Any) -> str | None:
    """The nearest ancestor of type ``epic``, walking through tickets the viewer may see only (an invisible parent ends
    the walk with ``None``), at most :data:`EPIC_LEVELS` levels."""
    key = view.fields.get("parent")
    for _ in range(EPIC_LEVELS):
        if not key:
            return None
        parent = _by_key(ctx).get(key)
        if parent is None or not ctx.viewer.sees(parent):
            return None
        if parent.type == "epic":
            return parent.key
        key = parent.fields.get("parent")
    return None


def _claim(view: Any, events: list[dict[str, Any]]) -> dict[str, Any] | None:
    c = view.claim
    if c is None:
        return None
    taken = next(
        (e for e in reversed(events) if e["type"] == "claim.taken" and e["actor"].get("session") == c.session), None
    )
    out: dict[str, Any] = {"session": c.session, "agent": ((taken or {}).get("actor") or {}).get("id") or ""}
    out["for"] = c.for_person
    if taken is not None:
        out["since"] = taken["at"]
    out["live"] = c.live
    return out


def _closed(events: list[dict[str, Any]]) -> dict[str, Any] | None:
    out = None
    for e in events:
        if e["type"] == "ticket.reopened":
            out = None
        elif e["type"] == "ticket.closed":
            out = {
                "resolution": e.get("resolution", "other"),
                "duplicate_of": e.get("duplicate_of"),  # the stored key only, never resolved
                "at": e["at"],
                "by": e["actor"].get("id"),
                "seq": e["seq"],
                "event_id": e["id"],
            }
    return out


def _row(ctx: ReadCtx, view: Any, updated: dict[str, Any], kinds: list[str], epic: str | None) -> dict[str, Any]:
    f = view.fields
    events: list[dict[str, Any]] = []
    if view.claim is not None or view.status == "closed":
        events = ctx.store.events(view.uid)
    row: dict[str, Any] = {
        "key": view.key,
        "uid": view.uid,
        "title": view.title,
        "type": view.type,
        "status": view.status,
        "priority": f.get("priority"),
        "size": f.get("size"),
        "labels": list(f.get("labels") or ()),
        "assignees": list(view.people.get("assignees", ())),
        "owner": view.owner,
        "parent": f.get("parent"),
        "epic": epic,
        "claim": _claim(view, events),
        "gates": {g: "n/a" if not x.applies else "approved" if x.approved else "open" for g, x in view.gates.items()},
        "needs": kinds,
        "updated_at": updated["at"],
        "head_seq": updated["seq"],
        "restricted": view.visibility != "workspace",
        "tasks": {
            "done": sum(t.state == "done" for t in view.tasks),
            "skipped": sum(t.state == "skipped" for t in view.tasks),
            "total": len(view.tasks),
        },
        "criteria": {"done": sum(bool(a.evidence) for a in view.acceptance), "total": len(view.acceptance)},
        "blocking_questions": sum(1 for q in view.questions if not q.answered and q.blocking),
    }
    if view.status == "closed":
        closed = _closed(events)
        if closed is not None:
            row["closed"] = closed
    return row


def _kinds(items: list[dict[str, Any]]) -> list[str]:
    have = {i["kind"] for i in items}
    return [k for k in KIND_ORDER if k in have]


def _matches(view: Any, args: Mapping[str, Any], updated_at: str, parent: str | None) -> bool:
    status = args.get("status")
    if status is None:
        if view.status == "closed":
            return False
    elif view.status not in status:
        return False
    if "type" in args and view.type not in args["type"]:
        return False
    if "label" in args and not set(args["label"]) & set(view.fields.get("labels") or ()):
        return False
    if "assignee" in args and args["assignee"] not in view.people.get("assignees", ()):
        return False
    if "owner" in args and args["owner"] != view.owner:
        return False
    if "parent" in args and parent != view.fields.get("parent"):
        return False
    return not ("updated_since" in args and updated_at < args["updated_since"])


def _text_match(ctx: ReadCtx, view: Any, needle: str) -> bool:
    if needle in _fold(view.key) or needle in _fold(view.title):
        return True
    try:
        sections = ctx.store.body_sections(view.uid)
    except StoreError:
        return False
    return any(needle in _fold(text) for text in sections.values())


def _sort(rows: list[dict[str, Any]], args: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Ties break by key ascending whatever ``order`` says; the sort is stable (``reverse`` keeps equal rows' order)."""
    rows = sorted(rows, key=lambda r: _key_order(r["key"]))
    sort, desc = args.get("sort", "updated"), args.get("order", "desc") == "desc"
    if sort == "updated":
        rows.sort(key=lambda r: r["updated_at"], reverse=desc)
    elif sort == "priority":
        rows.sort(key=lambda r: PRIORITY_RANK.get(r["priority"], len(PRIORITY_RANK)), reverse=desc)
    else:
        rows.sort(key=lambda r: _key_order(r["key"]), reverse=desc)
    return rows


def _first_page(ctx: ReadCtx) -> dict[str, Any]:
    """Read the state once: the snapshot data (filtered, sorted rows and each ticket's visibility), not yet stored."""
    args, viewer = ctx.args, ctx.viewer
    cursor = ctx.sub_cursor()  # before the state is read (H6a)
    parent = ctx.ticket(args["parent"]).key if "parent" in args else None
    epic_arg = ctx.ticket(args["epic"]).key if "epic" in args else None
    needle = _fold(args["q"]) if "q" in args else None
    rows: list[dict[str, Any]] = []
    for view in ctx.visible_tickets():
        updated = _last_event(ctx, view.uid)
        if not _matches(view, args, updated["at"], parent):
            continue
        epic = _epic(ctx, view)
        if epic_arg is not None and epic != epic_arg:
            continue
        items = ticket_items(ctx, view, full=False)
        if args.get("needs_me") and not (viewer.person is not None and any(viewer.person in i["who"] for i in items)):
            continue
        if needle is not None and not _text_match(ctx, view, needle):
            continue
        rows.append(_row(ctx, view, updated, _kinds(items), epic))
    return {"rows": _sort(rows, args), "sub_cursor": cursor, "as_of": ctx.as_of()}


def _current_row(ctx: ReadCtx, view: Any) -> dict[str, Any]:
    """The row of ``view`` from the state now (a snapshot uid's ticket, already checked visible to this viewer)."""
    items = ticket_items(ctx, view, full=False)
    return _row(ctx, view, _last_event(ctx, view.uid), _kinds(items), _epic(ctx, view))


@handler("ticket.query")
def ticket_query(ctx: ReadCtx) -> dict[str, Any]:
    args = ctx.args
    limit = args.get("limit", 50)
    if "page" in args:
        snap, start = ctx.pages.resume("ticket.query", args["page"], ctx.principal, ctx.epoch, ctx.viewer.person, args)
        if type(start) is not int or start < 0:
            raise HostError("page.expired", "the page token is not valid any more: restart from the first page")
        ctx.sub_cursor()  # a foreign append check, so "now" below is current
    else:
        snap, start = None, 0
    out: list[dict[str, Any]] = []
    total, nxt = 0, None
    if snap is None:  # first page: every row is visible to this viewer by construction
        data = _first_page(ctx)
        rows = data["rows"]
        total = len(rows)
        out = rows[:limit]
        nxt = limit if len(rows) > limit else None
        if nxt is not None:  # a snapshot only when a later page can follow: the uids, nothing else
            snap = ctx.pages.open(
                "ticket.query",
                ctx.principal,
                ctx.epoch,
                ctx.viewer.person,
                {"uids": [r["uid"] for r in rows], "sub_cursor": data["sub_cursor"], "as_of": data["as_of"]},
            )
    else:
        data = snap.data
        ctx.store.load_all()
        tickets = ctx.store.state.tickets  # one dict lookup per uid: the count below walks the whole snapshot
        for idx, uid in enumerate(data["uids"]):  # O(rows) per page: see the module docstring
            view = tickets.get(uid)
            if view is None or not ctx.viewer.sees(view):
                continue
            total += 1
            if idx < start:
                continue
            if len(out) < limit:
                out.append(_current_row(ctx, view))
            elif nxt is None:
                nxt = idx
    return {
        "rows": out,
        "next_page": ctx.pages.token(snap, nxt, args) if nxt is not None else None,
        "total": total,
        "sub_cursor": data["sub_cursor"],
        "as_of": data["as_of"],
    }
