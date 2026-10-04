from __future__ import annotations

from urllib.parse import urlencode

from fastapi import APIRouter, Query, Request
from fastapi.responses import RedirectResponse

from orch.clock import now as clock_now
from orch.core import events as events_mod
from orch.core import query, store
from orch.core.constants import PRIORITIES, PRIORITY_RANK, SIZES, STATUSES, TYPES
from orch.dashboard import launch
from orch.dashboard.data import agents
from orch.dashboard.data import cards as cards_data
from orch.dashboard.data import decisions as decisions_data
from orch.dashboard.data import epic as epic_data
from orch.dashboard.data import metrics, timeline, today
from orch.addons.runtime import clean_params
from orch.dashboard.views import as_list, page

router = APIRouter()

def _today_data(request: Request):
    """What Today and the one-by-one view share: one scan, one log read, one needs_you() and one waiting() per
    request, and the enriched decisions in list order (blocking, later, backlog)."""
    ws = request.app.state.ws
    entries = store.scan(ws)
    all_events = events_mod.read_events(ws)
    needs = query.needs_you(ws, entries=entries, events=all_events)
    at = clock_now()
    waiting = query.waiting(ws, entries=entries, events=all_events, needs=needs, now=at)
    all_decisions = decisions_data.decisions(ws, events=all_events, entries=entries, needs=waiting, now=at)
    # E2: the decisions of one epic stand together (Decision.group), on Today and in the one-by-one view alike
    all_decisions = [d for g in today.groups(all_decisions) for d in g["decisions"]]
    return ws, entries, all_events, needs, waiting, all_decisions, at


@router.get("/")
def decisions(request: Request, q: str = "", type_: str = Query("", alias="type"), priority: str = "",
              label: str = "", show_done: str = "", start: str = ""):
    # The board used to live at "/": old bookmarks with its filters still land on the board. An old ?kind= filter
    # is ignored: Today is one ordered list now (blocking first, then later, then the backlog line).
    if q or type_ or priority or label or show_done:
        return RedirectResponse("/board?" + request.url.query, status_code=303)
    ws, entries, all_events, needs, waiting, all_decisions, at = _today_data(request)
    rows = agents.agent_rows(ws, events=all_events, entries=entries, needs=needs, now=at)
    parts = today.split_today(all_decisions)  # M: combined req + plan approvals get their own "Ready to start"
    runtime = getattr(request.app.state, "addons", None)
    addon_inline, addon_loose = decisions_data.place_addon_decisions(runtime.decisions() if runtime else [],
                                                                     parts["blocking"] + parts["later"])
    settings = launch.load_settings()
    flight = today.in_flight(ws, needs=needs, entries=entries, events=all_events, now=at, rows=rows,
                             settings=settings)
    # One shared Start agent panel (not one per card): for ?start=<id> when it is an In flight card, else the first
    # card it can start on. Each card links to it; app.js swaps in another ticket's panel without a reload.
    startable = [c for c in flight[:today.FLIGHT_CARDS] if c["start"]]
    chosen = next((c for c in startable if c["id"].upper() == start.upper()), startable[0] if startable else None)
    start_panel = (_start_panel(ws, chosen["id"], needs=needs, rows=rows, now=at, settings=settings,
                                events=[e for e in all_events if e.ticket == chosen["id"]], request=request)
                   if chosen else None)
    # Agents now: compact ticket rows (data.cards) of the first four claimed tickets, from this request's data.
    builder = cards_data.Cards(ws, entries=entries, needs=needs, rows=rows, events=all_events, now=at,
                               reviews=runtime.review_index() if runtime else None)
    by_id = {e.id: e for e in entries}
    agent_cards = [c for r in rows[:4] if r.ticket in by_id and (c := builder.for_entry(by_id[r.ticket])) is not None]
    blocking = today.layout(parts["blocking"])
    later = today.layout(parts["later"])
    ready = today.layout(parts["ready"])
    return page(request, "today.html", nav="today", title="Today", needs=needs, waiting=waiting,
                all_decisions=all_decisions, blocking=today.groups(blocking["decisions"]), full=blocking["full"] | later["full"] | ready["full"],
                ready=ready["decisions"],
                stale=blocking["stale"][:today.STALE_ROWS], stale_total=len(blocking["stale"]),
                later=later["decisions"], backlog=parts["backlog"], kind_labels=decisions_data.KIND_LABELS, addon_inline=addon_inline,
                addon_loose=addon_loose, card_anchor=decisions_data.card_anchor,
                summary=today.ready_backlog(today.summary(ws, needs=waiting, rows=rows, now=at, decisions=all_decisions),
                                            len(parts["ready"])),
                working_rows=[r for r in rows if r.status == "working"], in_flight=flight, start_panel=start_panel,
                flight_cards=today.FLIGHT_CARDS,
                has_tickets=bool(entries), agent_cards=agent_cards,
                ticket_status_labels=metrics.STATUS_LABELS, ticket_status_roles=metrics.STATUS_ROLES,
                delegated_fyi=epic_data.delegated_fyi(ws, entries, all_events),
                phone_receipts=timeline.phone_receipts(ws, all_events, now=at),
                backlog_total=sum(1 for e in entries if e.status == "backlog"),
                away=today.away(ws, events=all_events, entries=entries, now=at), away_hours=today.AWAY_HOURS)


def _start_panel(ws, ref: str, *, needs, rows, now, settings, events=None, request=None):
    from orch.dashboard.data import agent_start
    from orch.errors import OrchError
    try:
        _, t = store.load(ws, ref)
    except OrchError:
        return None
    s = agent_start.suggest(ws, t, needs_items=needs, rows=rows, now=now, settings=settings, events=events,
                            request=request)
    return s if s and not s.get("disabled") else None


@router.get("/t/{ref}/agent/panel")
def start_panel(request: Request, ref: str, next: str = "/"):
    """The Start agent panel of one ticket as an HTML fragment, for Today's shared panel (app.js swaps it in)."""
    from fastapi.responses import HTMLResponse

    from orch.dashboard.views import TEMPLATES, safe_next
    ws = request.app.state.ws
    needs = query.needs_you(ws)
    at = clock_now()
    s = _start_panel(ws, ref, needs=needs, rows=agents.agent_rows(ws, needs=needs, now=at), now=at,
                     settings=launch.load_settings(), request=request)
    html = TEMPLATES.get_template("_start_agent_panel.html").render(s=s, ref=ref, next=safe_next(next) or "/")
    return HTMLResponse(html, headers={"Cache-Control": "no-store"})


# Words a decision answers to in the command palette, besides its label (app.js maps synonyms such as "ok" or
# "sign off" onto these).
_PALETTE_WORDS = {"approve-requirements": "approve requirements", "approve-plan": "approve plan",
                  "re-approve": "approve re-approve", "answer": "answer question", "confirm": "answer confirm",
                  "verdict": "verdict accept done", "task": "task", "stale-claim": "release claim stale",
                  "broken": "repair"}


@router.get("/palette.json")
def palette(request: Request):
    """The command palette's data (Ctrl+K): the decisions waiting on the human (blocking and later; backlog grooming
    stays on its own page) and every ticket. Read-only; picking a decision only opens and arms its card."""
    from fastapi.responses import JSONResponse
    ws = request.app.state.ws
    entries = store.scan(ws)
    items = [i for i in query.waiting(ws, entries=entries) if i.get("scope") != "backlog"]
    decisions = []
    for i in items:
        card = f"d-{i['ticket']}-{i['kind']}" + (f"-{i['detail']}" if i["kind"] == "task" else "")
        label = decisions_data.KIND_LABELS.get(i["kind"], i["kind"]) + (f" {i['detail']}" if i["kind"] == "re-approve" else "")
        decisions.append({"label": f"{label} · {i['ticket']}", "card": card, "href": f"/#{card}",
                          "words": f"{_PALETTE_WORDS.get(i['kind'], '')} {i.get('title') or ''}"})
    tickets = [{"id": e.id, "title": (e.meta or {}).get("title") or "", "status": e.status}
               for e in sorted(entries, key=lambda e: (e.status == "done", e.id))][:1000]
    return JSONResponse({"decisions": decisions, "tickets": tickets}, headers={"Cache-Control": "no-store"})


_FOCUS_TITLES = {"backlog": "Groom the backlog", "blocking": "Decide one by one", "later": "When you have a moment"}


@router.get("/groom")
def groom(request: Request, at: str = "", scope: str = "backlog"):
    """One decision at a time (#11/#17), with its full gated text, Previous and Next: the backlog (grooming), or the
    blocking or later decisions that Today shows as one-line rows. Silent claims are released from Today or Activity."""
    scope = scope if scope in _FOCUS_TITLES else "backlog"
    ws, entries, all_events, needs, waiting, all_decisions, now = _today_data(request)
    shown = [d for d in today.split(all_decisions)[scope] if d.kind != "stale-claim"]
    i, d, prev, nxt = today.neighbours(shown, at)
    return page(request, "groom.html", nav="today", title=_FOCUS_TITLES[scope], needs=needs, waiting=waiting,
                d=d, index=i, total=len(shown), prev=prev, next_id=nxt, scope=scope, heading=_FOCUS_TITLES[scope],
                kind_labels=decisions_data.KIND_LABELS, addon_inline={}, card_anchor=decisions_data.card_anchor)


def _repo_names(meta: dict) -> list[str]:
    names = []
    for r in as_list(meta.get("repos")):
        if isinstance(r, str):
            names.append(r)
        elif isinstance(r, dict) and r.get("name"):
            names.append(str(r["name"]))
    return names


def _external_keys(meta: dict) -> list[str]:
    """External keys as strings; hand-edited files may hold a number or other odd value."""
    keys = []
    for x in as_list(meta.get("external")):
        if not isinstance(x, dict):
            continue
        key = x.get("key")
        if key is None:
            continue
        key = key if isinstance(key, str) else str(key)
        if key:
            keys.append(key)
    return keys


def _matches_external(value: str, keys: list[str]) -> bool:
    needle = value.lower()
    for key in keys:
        key_l = key.lower()
        if "-" in needle:
            if needle == key_l:
                return True
        elif key_l.split("-", 1)[0] == needle:
            return True
    return False


_LIST_COLUMNS = (("key", "Key"), ("title", "Title"), ("move", "Move"), ("progress", "Progress"), ("code", "PR"),
                 ("size", "Size"), ("updated", "Updated"))
_SORTABLE = ("key", "title", "move", "size", "updated")
_SIZE_RANK = {s: i for i, s in enumerate(SIZES)}


def _sort_key(column: str):
    """Sort value for one List column; unknown values sort last, ties fall back to the key. "move" is the
    default: your move first (most urgent kind, then oldest), then stale, blocked, working, ready, done."""
    def key(c: dict):
        if column == "move":
            return cards_data.sort_key(c)
        if column == "size":
            value = _SIZE_RANK.get(c["size"], len(_SIZE_RANK)) if isinstance(c["size"], str) else len(_SIZE_RANK)
        elif column == "key":
            value = c["id"]
        elif column == "updated":
            value = c["updated"] or "\uffff"
        else:
            value = str(c.get(column) or "\uffff").lower()
        return (value, c["id"])
    return key


# M (v4): the Board's agent flow: four lanes, empty ones drawn as narrow rails; backlog is a drawer below, Done a rail.
FLOW = ("open", "in-progress", "waiting", "testing")
FLOW_LABELS = {"open": "Ready", "in-progress": "Working", "waiting": "Waiting", "testing": "Testing"}
FLOW_ICONS = {"open": "neu", "in-progress": "info", "waiting": "neu", "testing": "info"}
STRIP_MAX = 8  # Your move cards drawn on the Board (4 per row); the rest are one link away in the List
DONE_DAYS = 7


def your_moves(columns: dict) -> list[dict]:
    """M: every card whose move is the human's (any status but done), most urgent first: the Your move strip. The
    cards stay in their lanes too (with the pink move chip), so the flow below is complete."""
    return sorted((c for status, cards in columns.items() if status != "done" for c in cards
                   if (c.get("move") or {}).get("who") == "you"), key=cards_data.sort_key)


def _counts(d) -> str:
    """"4 req · 3 steps": the size of what a Review & approve opens, from the gated sections' list items."""
    from orch.dashboard.data.story import items
    parts = list(d.gate_parts or []) + list((d.together or {}).get("gate_parts") or [])
    req = sum(items(p["text"]) for p in parts if p["name"] == "Requirements")
    plan = sum(items(p["text"]) for p in parts if p["name"] == "Plan")
    return " · ".join(x for x in (f"{req} req" if req else "", f"{plan} step{'s' if plan != 1 else ''}" if plan else "") if x)


PLAN_INLINE_MAX = 700  # a plan this short is shown in full on its strip card and approved there; longer ones open


def strip(ws, cards: list[dict], *, waiting, entries, events, now) -> list[dict]:
    """The first STRIP_MAX Your move cards with the Decision each acts on (data.decisions, built only for these):
    [{card, d, counts, inline_plan}]. `d` is None for a move without a needs item (an epic child the epic's
    re-approval covers): the strip card then only links to the ticket."""
    shown = cards[:STRIP_MAX]
    # the item whose form is drawn is the one the card's move chip names (a repair card's move is "repair", its item
    # kind "broken"); a ticket's first other item only when none matches
    want = {c["id"].upper(): ("broken" if c["move"]["what"] == "repair" else c["move"]["what"]) for c in shown}
    chosen: dict[str, dict] = {}
    for i in waiting:
        tid = str(i.get("ticket", "")).upper()
        if tid not in want or i.get("kind") in ("confirm", "stale-claim"):
            continue
        if tid not in chosen or (i.get("kind") == want[tid] and chosen[tid].get("kind") != want[tid]):
            chosen[tid] = i
    items = list(chosen.values())
    found = {d.ticket.upper(): d for d in decisions_data.decisions(ws, events=events, entries=entries, needs=items,
                                                                   now=now)} if items else {}
    out = []
    for c in shown:
        d = found.get(c["id"].upper())
        plan_text = sum(len(p["text"]) for p in (d.gate_parts or [])) if d is not None else 0
        # an open-question line needs the override box: that plan opens in full on the ticket instead
        # R25: never act in place on a ticket whose approval the ledger on this machine does not hold
        inline_plan = (d is not None and not d.unsigned and d.kind == "approve-plan" and not d.hint and not d.diff
                       and not d.charter
                       and not d.question
                       and d.gate_parts is not None and not any(p["raw"] for p in d.gate_parts)
                       and plan_text <= PLAN_INLINE_MAX)
        out.append({"card": c, "d": d, "counts": _counts(d) if d is not None else "", "inline_plan": inline_plan})
    return out


def priority_counts(cards: list[dict]) -> list[dict]:
    """The Backlog drawer's summary: [{priority, n, hot}] most urgent first, only priorities that occur."""
    n = {p: 0 for p in PRIORITY_RANK}
    for c in cards:
        p = c.get("priority") if c.get("priority") in n else "normal"
        n[p] += 1
    return [{"priority": p, "n": n[p], "hot": p in ("urgent", "high")}
            for p in sorted(n, key=PRIORITY_RANK.get) if n[p]]


def done_this_week(done: list[dict], now) -> int:
    from datetime import timedelta

    from orch.dashboard.data.steps import when
    since = now - timedelta(days=DONE_DAYS)
    return sum(1 for c in done if (at := when(c.get("updated"))) is not None and at >= since)


def _agent_created(events) -> set[str]:
    """Ids of the tickets an agent created (their single `ticket.created` event), for the Backlog's idea mark."""
    by_ticket: dict[str, list] = {}
    for e in events:
        if e.kind == "ticket.created" and e.ticket:
            by_ticket.setdefault(e.ticket, []).append(e)
    # the rule of orch.core.epics.created_by_agent: exactly one creation event, by an agent
    return {t for t, made in by_ticket.items() if len(made) == 1 and str(made[0].actor).startswith("agent:")}


@router.post("/board/backlog")
async def board_backlog(request: Request):
    """C: fold or unfold the Board's Backlog lane and remember it (the fold's toggle; a plain form without JS)."""
    import asyncio

    from orch.dashboard import prefs
    form = await request.form()
    await asyncio.to_thread(prefs.set_backlog_open, request.app.state.ws, form.get("open") == "1")
    return RedirectResponse("/board", status_code=303)


@router.get("/board")
def board(request: Request, q: str = "", type_: str = Query("", alias="type"), priority: str = "",
          label: str = "", show_done: int = 0, repo: str = "", external: str = "", view: str = "board",
          sort: str = "move", dir_: str = Query("asc", alias="dir"), group: str = "", backlog: str = "",
          status: str = ""):
    from orch.dashboard import prefs
    ws = request.app.state.ws
    # C: the Backlog lane starts folded; ?backlog=open|closed (and the fold's toggle, POST /board/backlog) remembers
    # the choice per user, outside the repository.
    if backlog in ("open", "closed"):
        prefs.set_backlog_open(ws, backlog == "open")
    backlog_open = prefs.backlog_open(ws)
    # Group by (E2): remembered per workspace outside the repository; a `group` in the URL changes it.
    if group in prefs.GROUPS:
        prefs.set_group_by(ws, group)
    group_by = group if group in prefs.GROUPS else prefs.group_by(ws)
    # The External tab exists only while an enabled addon fills the board.external slot.
    external_tab = request.app.state.addons.declares("board.external")
    view = view if view in ("list", "external") else "board"
    if view == "external" and not external_tab:
        view = "board"
    sort = sort if sort in _SORTABLE else "move"
    dir_ = "desc" if dir_ == "desc" else "asc"
    entries = query.list_tickets(ws, label=label or None)
    status = status if status in STATUSES else ""
    if status:  # M: the Done rail opens the List on the done tickets
        entries = [e for e in entries if e.status == status]
        show_done = 1 if status == "done" else show_done
    if q:
        hits = {e.id for e in query.search(ws, q)}
        entries = [e for e in entries if e.id in hits]
    all_entries = store.scan(ws)
    all_events = events_mod.read_events(ws)
    needs = query.needs_you(ws, entries=all_entries, events=all_events)
    waiting = query.waiting(ws, entries=all_entries, events=all_events, needs=needs)
    runtime = getattr(request.app.state, "addons", None)
    # One card model per ticket (data.cards): the move chip, gates, progress, PR and agent, from this request's scan.
    builder = cards_data.Cards(ws, entries=all_entries, needs=needs, events=all_events,
                               reviews=runtime.review_index() if runtime is not None else None,
                               mentions=runtime.mention_index() if runtime is not None else None)
    columns: dict[str, list[dict]] = {s: [] for s in STATUSES}
    items: list[tuple] = []  # (entry, card) in column order, for Group by
    for e in entries:
        meta = e.meta or {}
        if type_ and meta.get("type") != type_:
            continue
        if priority and meta.get("priority") != priority:
            continue
        if repo and repo not in _repo_names(meta):
            continue
        external_keys = _external_keys(meta)
        if external and not _matches_external(external, external_keys):
            continue
        if e.status == "done" and not show_done:
            columns["done"].append({"id": e.id, "updated": (e.meta or {}).get("updated")})  # counted (the Done rail)
            items.append((e, columns["done"][-1]))
            continue
        card = builder.for_entry(e)
        if card is not None:
            columns[e.status].append(card)
            items.append((e, card))
    if view == "board" and not q:
        # M: the Your move strip on top (data from `move`); the backlog by priority, then age, in a drawer below; an
        # agent's idea is marked as such.
        created = {e.id: str((e.meta or {}).get("created") or "") for e in all_entries}
        ideas = _agent_created(all_events)
        for c in columns["backlog"]:
            if c.get("id") in ideas:
                c["agent_idea"] = True
        columns["backlog"].sort(key=lambda c: (PRIORITY_RANK.get(c.get("priority"), 2), created.get(c["id"], ""), c["id"]))
    repos = sorted((ws.config.get("git", {}).get("repos") or {}).keys())
    filters = [(k, v) for k, v in (("q", q), ("type", type_), ("priority", priority), ("label", label),
                                    ("repo", repo), ("external", external), ("status", status)) if v]
    active = sum(len(cards) for status, cards in columns.items() if status != "done")
    list_rows = [c for s in STATUSES if s != "done" or show_done for c in columns[s]]
    list_rows.sort(key=_sort_key(sort if view == "list" or not q else "move"), reverse=dir_ == "desc" and view == "list")
    groups = []
    if group_by != "none" and not (q and view == "board"):
        groups = epic_data.lanes(ws, group_by, items, builder.epics)
        for g in groups:
            g["rows"] = sorted((c for c in g["cards"] if "move" in c and (c["status"] != "done" or show_done)),
                               key=_sort_key(sort), reverse=dir_ == "desc")

    moves, strip_cards = [], []
    at = clock_now()
    if view == "board" and not q:
        moves = your_moves(columns)
        strip_cards = strip(ws, moves, waiting=waiting, entries=all_entries, events=all_events, now=at)
    for g in groups:
        g["backlog_counts"] = priority_counts(g["columns"].get("backlog") or [])
        g["done_week"] = done_this_week(g["columns"].get("done") or [], at) if not show_done else 0

    # Switching tabs keeps the filters, show_done and the List sort (a non-default one rides along
    # on the Board URL too, so going back to List finds it again).
    sorting = [("sort", sort), ("dir", dir_)] if (sort, dir_) != ("move", "asc") else []
    keep = filters + ([("show_done", 1)] if show_done else []) + sorting

    def list_link(column: str) -> str:
        """The List view sorted by `column`; the sorted column flips direction."""
        flip = "desc" if column == sort and dir_ == "asc" else "asc"
        extra = [("show_done", 1)] if show_done else []
        return "/board?" + urlencode(filters + extra + [("view", "list"), ("sort", column), ("dir", flip)])

    return page(request, "board.html", nav="board", title="Board", needs=needs, waiting=waiting, columns=columns,
                statuses=STATUSES, backlog_open=backlog_open, board_roles={**metrics.STATUS_ROLES, "waiting": "neu"}, types=TYPES, priorities=PRIORITIES, repos=repos, q=q, type=type_, priority=priority,
                label=label, repo=repo, external=external, show_done=bool(show_done), view=view, sort=sort, dir=dir_,
                active_count=active, external_tab=external_tab, external_link="/board?view=external",
                external_params=clean_params(request.query_params, drop=("view",)) if view == "external" else {}, list_rows=list_rows, list_columns=_LIST_COLUMNS, list_sortable=_SORTABLE, list_link=list_link,
                status_labels=metrics.STATUS_LABELS, status_roles=metrics.STATUS_ROLES,
                group_by=group_by, groups=groups, group_labels=epic_data.GROUP_LABELS,
                board_link="/board" + ("?" + urlencode(keep) if keep else ""),
                list_view_link="/board?" + urlencode(filters + [("view", "list")] + keep[len(filters):]),
                moves=moves, strip=strip_cards, strip_max=STRIP_MAX, kind_labels=decisions_data.KIND_LABELS, flow=FLOW, flow_labels=FLOW_LABELS,
                flow_icons=FLOW_ICONS, backlog_counts=priority_counts(columns["backlog"]),
                done_week=done_this_week(columns["done"], at) if not show_done else 0, done_days=DONE_DAYS,
                done_list_link="/board?" + urlencode([("view", "list"), ("status", "done")]),
                done_link="?" + urlencode(filters + ([("view", "list")] if view == "list" else []) + sorting
                                          + [("show_done", 1)]))
