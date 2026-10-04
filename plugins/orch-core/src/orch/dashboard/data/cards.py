"""One ticket, one model (ticket design review §3.2, design-system TicketCard): `ticket_card()` derives, by rules
only, what every surface shows about a ticket. The board card (M), the compact row (S: List, Today side lists) and
the ticket page header (L) draw the same dict with the macros in `_ticket_card.html`.

    move    whose move it is and what: {who: you|agent|nobody, what, ref, label, role, icon, since}, plus `why`
            (one line, rule text) when who = you
            what: approve-requirements | approve-plan | re-approve | answer | task | verdict | repair
                  | working | stale | blocked | ready | done. The role is `you` (pink) only when who = you.
    gates   requirements / plan: {state: approved|you|pending|invalidated|changes|skipped, glyph, role, word}
    tasks   {done, total, closed, doing, blocked, human, ...task bar data} or None (no tasks, or unreadable)
    ac      {proven, total}: criteria with a Verification line citing them (orch.core.evidence)
    code    the main PR {label, url, checks: passed|failed|pending|none|unknown, role, draft, others} or None;
            from the reviews addon (LinkIndex) when it knows the PR, else from the ticket's own links
    agent   {harness, status: working|waiting|stale, since, last, last_action} or None
    left    the "what is left" rule text
    parent  the parent ticket id when it is not an epic (a follow-up's source), or None
    epic    the epic the ticket belongs to {id, title, status}, or None (shown above the title)
    rollup  epics only: {done, total, testing, needs_you, ac_proven, ac_total} over the children, else None

No agent prose ever lands in a slot of this model. A request builds one `Cards` and reuses its scan, needs,
agent rows, events and review index for every card."""
from __future__ import annotations

import re
import time
from collections import OrderedDict
from datetime import datetime

from orch.clock import now as clock_now
from orch.core import evidence, query, store
from orch.core.artifacts import entries as artifact_entries
from orch.core import tasks as tk
from orch.core.gates import changes_pending, gate_state
from orch.core.lifecycle import unanswered_blocking
from orch.dashboard.data import tasks as tasks_data
from orch.dashboard.data.metrics import STATUS_LABELS
from orch.dashboard.data.steps import _span, need_gate, when, why_waiting
from orch.errors import TicketParseError

ICONS = {"ok": "✓", "info": "◐", "you": "●", "warn": "▲", "err": "✕", "neu": "○"}
GATE_GLYPH = {"approved": ("✓", "ok", "approved"), "you": ("●", "neu", "waits for you"),
              "pending": ("○", "neu", "not approved yet"), "invalidated": ("▲", "warn", "changed since approval"),
              "changes": ("▲", "warn", "changes requested"), "skipped": ("–", "neu", "skipped for this size"),
              "none": ("–", "neu", "no plan: an epic's children have plans")}
CHECKS = {"passed": ("✓", "ok"), "failed": ("✕", "err"), "pending": ("◐", "info"), "cancelled": ("○", "neu"),
          "none": ("", "neu"), "unknown": ("", "neu")}
# Sort groups for "your move first": the human's moves, then what needs attention, then the rest.
_GROUP = {"you": 0, "stale": 1, "blocked": 2, "working": 3, "ready": 4, "done": 5}
STATIC_MAX = 4096
_STATIC: OrderedDict = OrderedDict()  # (file stamp, plan_skip_sizes, own needs) -> the static part of a card
_PR_NUMBER = re.compile(r"/(?:pull|pulls|pr|merge_requests)/(\d+)(?:[/?#]|$)")


class Card(dict):
    """A card dict whose keys also read as attributes. Jinja looks `c.move.label` up as an attribute first; on a
    plain dict that lookup fails and falls back to the item every time, which was most of the Board's render time."""
    __slots__ = ()

    def __getattr__(self, name):
        try:
            return self[name]
        except KeyError:
            raise AttributeError(name) from None


def _card(card: dict) -> Card:
    """The card and the parts templates walk into as Cards (shallow: other values stay as they are)."""
    out = Card(card)
    for key in ("move", "tasks", "code", "agent", "ac", "external", "epic", "rollup"):
        if type(out.get(key)) is dict:
            out[key] = Card(out[key])
    if type(out.get("gates")) is dict:
        out["gates"] = Card({g: Card(v) for g, v in out["gates"].items()})
    return out


def _list(value) -> list:
    """A frontmatter list as a list; a hand-edited file may hold a string, a number or a mapping instead."""
    return value if isinstance(value, list) else []


def pr_number(url) -> str | None:
    m = _PR_NUMBER.search(str(url or ""))
    return m.group(1) if m else None


def _first_external(meta: dict) -> dict | None:
    for x in _list(meta.get("external")):
        if isinstance(x, dict) and x.get("key") not in (None, ""):
            url = x.get("url")
            return {"key": str(x["key"]), "url": url if isinstance(url, str) and url.lower().startswith(("http://", "https://")) else None}
    return None


def _move(kind: str, label: str, *, who: str, role: str, ref=None, since=None) -> dict:
    return {"who": who, "what": kind, "ref": ref, "label": label, "role": role, "icon": ICONS[role], "since": since}


def _human_move(t, item: dict) -> dict:
    kind, detail = item.get("kind"), str(item.get("detail") or "")
    since = when(t.meta.get("updated"))
    if kind == "answer":
        qids = [q.strip() for q in detail.split(",") if q.strip()]
        return _move("answer", "Answer " + ", ".join(qids[:2]) + (f" +{len(qids) - 2}" if len(qids) > 2 else ""),
                     who="you", role="you", ref=qids[0] if qids else None, since=since)
    if kind == "task":
        return _move("task", f"Do {detail}", who="you", role="you", ref=detail, since=since)
    if kind in ("approve-requirements", "approve-plan"):
        gate = kind.removeprefix("approve-")
        label = "Approve requirements + plan" if item.get("together") else f"Approve {gate}"  # F2: one confirm
        return _move(kind, label, who="you", role="you", ref=gate, since=since)
    if kind == "re-approve":
        gate = need_gate(item) or detail
        return _move("re-approve", f"Re-approve {gate}", who="you", role="you", ref=gate, since=since)
    if kind == "approve-epic":
        return _move("approve-epic", "Re-approve the epic", who="you", role="you", ref="requirements", since=since)
    if kind == "verdict":
        return _move("verdict", "Verdict", who="you", role="you", since=since)
    return _move("repair", "Repair", who="you", role="you", since=since)


def _gates(t, own: list[dict], plan_skip_sizes) -> dict:
    awaited = {need_gate(i) for i in own} - {None}
    out = {}
    for gate in ("requirements", "plan"):
        state = gate_state(t, gate)
        if gate in awaited:
            shown = "you"
        elif state == "approved":
            shown = "approved"
        elif changes_pending(t, gate):
            shown = "changes"
        elif state == "invalidated":
            shown = "invalidated"
        elif gate == "plan" and t.meta.get("type") == "epic":
            shown = "none"
        elif gate == "plan" and t.meta.get("size") in tuple(plan_skip_sizes):
            shown = "skipped"
        else:
            shown = "pending"
        glyph, role, word = GATE_GLYPH[shown]
        out[gate] = {"state": shown, "glyph": glyph, "role": role, "word": word}
    return out


def _tasks(items) -> dict | None:
    if not items:
        return None
    s = tk.summary(items)
    human = sum(1 for x in items if x.owner == "human" and x.state in tk.OPEN_STATES)
    return {**{k: s[k] for k in ("done", "total", "closed", "doing", "blocked", "todo", "skipped")}, "human": human,
            "open": tk.open_ids(items), "progress": tasks_data.progress(s),
            "segments": tasks_data._segments([x.state for x in items]), "bar": tasks_data.bar(s)}


def _code(t, reviews: list[dict]) -> dict | None:
    """The main PR: the reviews addon's view of it when there is one (checks, draft), else the first linked PR."""
    linked = [p for p in _list(t.meta.get("prs")) if isinstance(p, dict) and isinstance(p.get("url"), str)]
    items = [i for i in reviews if isinstance(i, dict) and isinstance(i.get("url"), str)]
    if items:
        main = items[0]
        number = main.get("number") if isinstance(main.get("number"), int) else pr_number(main["url"])
        checks = (main.get("checks") or {}).get("state") if isinstance(main.get("checks"), dict) else None
        checks = checks if checks in CHECKS else "unknown"
        draft = main.get("draft") is True
        state = str(main.get("state") or "open")
        urls = {i["url"] for i in items} | {p["url"] for p in linked}
    elif linked:
        main = linked[0]
        number, checks, draft = pr_number(main["url"]), "unknown", False
        state = str(main.get("state") or "")
        state = "" if state == "draft" else state  # `orch link` writes "draft" as a placeholder nobody refreshes
        urls = {p["url"] for p in linked}
    else:
        return None
    url = main["url"] if main["url"].lower().startswith(("http://", "https://")) else None
    glyph, role = CHECKS[checks]
    if draft and checks in ("none", "unknown"):
        glyph, role = "○", "neu"
    return {"label": f"PR #{number}" if number else "PR", "url": url, "checks": checks, "draft": draft, "state": state,
            "glyph": glyph, "role": role, "others": len(urls) - 1}


def _mentioned(items) -> list[dict]:
    """PRs that only name the ticket in their description: shown as "mentioned in PR #12", never as its main PR."""
    out = []
    for i in items:
        if isinstance(i, dict) and isinstance(i.get("url"), str):
            number = i.get("number") if isinstance(i.get("number"), int) else pr_number(i["url"])
            url = i["url"] if i["url"].lower().startswith(("http://", "https://")) else None
            out.append({"label": f"PR #{number}" if number else "PR", "url": url})
    return out


def _left(t, card: dict) -> str:
    """Chapter 5 of the story, rule text only."""
    status = t.status
    if status == "done":
        return "Nothing left."
    if t.meta.get("type") == "epic":
        if status == "backlog":
            return "Your approval of the epic and its children, then the children are worked"
        return "Its children worked to testing, then your verdict (per child, or once for the epic)"
    ac_todo = card["ac"]["total"] - card["ac"]["proven"]
    prove = f"{ac_todo} {'criterion' if ac_todo == 1 else 'criteria'} to prove" if ac_todo else ""
    if status == "testing":
        return " · ".join(p for p in (prove, "your verdict") if p)
    if status == "backlog":
        return ("Your approval of the requirements, then the work" if card["gates"]["requirements"]["state"] == "you"
                else "Requirements to refine, then your approval, then the work")
    parts: list[str] = []
    open_qs = [str(q.get("id")) for q in unanswered_blocking(t)]
    if open_qs:
        parts.append("your answer to " + ", ".join(open_qs))
    plan = card["gates"]["plan"]["state"]
    if plan == "you":
        parts.append("your plan approval")
    elif plan in ("pending", "invalidated", "changes"):
        parts.append("a plan, then your approval")
    tasks = card["tasks"]
    if status == "open" and not tasks:
        parts.insert(0, "an agent claims it")
    if tasks and tasks["open"]:
        ids = tasks["open"]
        parts.append((", ".join(ids) if len(ids) <= 4 else f"{len(ids)} tasks") + " open")
    elif not tasks:
        parts.append("the task list")
    parts.append("then testing")
    if prove:
        parts.append(prove)
    parts.append("then your verdict")
    text = " · ".join(parts)
    return text[:1].upper() + text[1:]


def sort_key(card: dict):
    """Your move first (by urgency, then oldest), then stale, blocked, working, ready, done."""
    move = card["move"]
    group = _GROUP["you"] if move["who"] == "you" else _GROUP.get(move["what"], 4)
    kind = query.NEEDS_ORDER.get(move["what"] if move["what"] != "repair" else "broken", 9) if move["who"] == "you" else 0
    since = move["since"] or card.get("updated_at")
    return (group, kind, since.timestamp() if isinstance(since, datetime) else float("inf"), card["id"])


class Cards:
    """Builds ticket cards for one request. Everything per workspace (scan, needs_you, agent rows, events, the
    reviews index) is computed once, lazily, and shared by every card."""

    def __init__(self, ws, *, entries=None, needs=None, rows=None, events=None, reviews=None, mentions=None, now=None):
        self.ws = ws
        self.now = now or clock_now()
        self._entries, self._needs, self._rows, self._events = entries, needs, rows, events
        self._reviews = reviews  # {ticket id: [review items]} or a callable(ticket id) -> items
        self._mentions = mentions or {}  # {ticket id: [review items that only name it in their description]}
        self.skip = tuple(ws.config["gates"]["plan_skip_sizes"])
        self._by_ticket_events: dict | None = None
        self._rows_by: dict | None = None
        self._needs_by: dict | None = None
        self._epics: dict | None = None
        self._signed: list | None = None

    @property
    def entries(self):
        if self._entries is None:
            self._entries = store.scan(self.ws)
        return self._entries

    @property
    def needs(self):
        if self._needs is None:
            self._needs = query.needs_you(self.ws, entries=self.entries, events=self._events)
        return self._needs

    @property
    def events(self):
        if self._events is None:
            from orch.core.events import read_events
            self._events = read_events(self.ws)
        return self._events

    @property
    def rows(self):
        if self._rows is None:
            from orch.dashboard.data.agents import agent_rows
            self._rows = agent_rows(self.ws, now=self.now, events=self.events, entries=self.entries, needs=self.needs)
        return self._rows

    def _row(self, tid: str):
        if self._rows_by is None:
            self._rows_by = {r.ticket.upper(): r for r in self.rows}
        return self._rows_by.get(tid.upper())

    def _own(self, tid: str) -> list[dict]:
        """This ticket's needs_you items: one index per request instead of a pass over every need per card."""
        if self._needs_by is None:
            self._needs_by = {}
            for item in self.needs:
                if item.get("kind") in ("confirm", "stale-claim"):  # never the human's move (steps._own)
                    continue
                self._needs_by.setdefault(str(item.get("ticket", "")).upper(), []).append(item)
        return self._needs_by.get(tid.upper(), [])

    @property
    def epics(self) -> dict:
        """{EPIC ID: {id, title, status}} of this request's scan (data.epic.epic_index)."""
        if self._epics is None:
            from orch.dashboard.data.epic import epic_index
            self._epics = epic_index(self.entries)
        return self._epics

    def _reviews_of(self, tid: str) -> list[dict]:
        if self._reviews is None:
            return []
        if callable(self._reviews):
            return list(self._reviews(tid) or [])
        return list(self._reviews.get(tid, []))

    def _last_action(self, tid: str, actor_prefix: str = "agent:") -> str | None:
        from orch.dashboard.data.timeline import action_phrase
        if self._by_ticket_events is None:
            self._by_ticket_events = {}
            for ev in self.events:
                self._by_ticket_events.setdefault(ev.ticket, []).append(ev)
        mine = [e for e in self._by_ticket_events.get(tid, []) if str(e.actor).startswith(actor_prefix)]
        return action_phrase(mine[-1]) if mine else None  # a fixed phrase: no agent text in a status slot

    def _uncovered(self, t) -> tuple[str, str] | None:
        """(gate, epic id) of a child's gate that counted through its epic's signed charter and is no longer covered
        (its text changed since the epic was approved): no needs_you item of its own (the epic's re-approval is the
        decision, query.needs_you), yet the agent may not go on (ledger.require_signed). Else None."""
        if not t.meta.get("parent") or t.status == "done":
            return None
        gates = t.meta.get("gates") or {}
        candidates = [g for g in ("requirements", "plan") if (gates.get(g) or {}).get("approved")
                      and (gates.get(g) or {}).get("epic")]
        if not candidates:
            return None
        from orch.core import ledger
        if self._signed is None:
            self._signed = ledger.entries(self.ws)
        for gate in candidates:
            if gate_state(t, gate) == "invalidated" or not ledger._charter_current(self.ws, t, gate, self._signed):
                return gate, str(gates[gate].get("epic"))
        return None

    def for_entries(self, entries) -> list[dict]:
        return [c for c in (self.for_entry(e) for e in entries) if c is not None]

    def for_entry(self, entry) -> dict | None:
        """The card of a scanned file; a file that does not parse gets a Repair card."""
        if entry.meta is None:
            return self.broken(entry.id, entry.status, entry.error or "")
        try:
            t = store.read_ticket(entry.path, shared=True)  # read-only: no copy per card
        except (OSError, TicketParseError, UnicodeDecodeError) as e:
            return self.broken(entry.id, entry.status, getattr(e, "message", str(e)))
        return self.for_ticket(t, entry=entry)

    def broken(self, tid: str, status: str, error: str) -> dict:
        return _card({"id": tid, "title": f"⚠ {error}" if error else "⚠ cannot be read", "status": status,
                "status_label": STATUS_LABELS.get(status, status), "size": "", "priority": "", "hot": False,
                "type": "", "external": None, "externals": [], "parent": None, "epic": None, "rollup": None,
                "labels": [], "repos": [],
                "updated": "", "updated_at": None,
                "move": _move("repair", "Repair", who="you", role="you"), "gates": None, "tasks": None,
                "ac": {"proven": 0, "total": 0}, "code": None, "mentions": [], "agent": None, "left": "Repair the file.",
                "blockers": [], "broken": True})

    def for_ticket(self, t, *, entry=None, tasks=None) -> dict:
        tid, meta, status = t.id, t.meta, t.status
        own = self._own(tid)
        if tasks is None:
            if entry is not None:
                tasks = tasks_data.entry_tasks(entry)
            else:
                try:
                    tasks = tk.ticket_tasks(t)
                except tk.TaskParseError:
                    tasks = None
        row = self._row(tid) if isinstance(meta.get("claim"), dict) and meta["claim"].get("session") else None
        blockers = query.open_blockers(self.ws, t, self.entries) if status != "done" and meta.get("blocked_by") else []
        if status == "done":
            move = _move("done", "Done", who="nobody", role="ok", since=when(meta.get("updated")))
        elif own:
            move = _human_move(t, own[0])
            move["why"] = why_waiting(own[0])  # F4: one line saying why it waits on the human
        elif (uncovered := self._uncovered(t)) is not None:
            gate, eid = uncovered
            move = _move("re-approve", f"Re-approve epic {eid}", who="you", role="you", ref=gate,
                         since=when(meta.get("updated")))
            move.update(epic=eid, why=f"The {gate} changed after you approved epic {eid}; the work waits until you "
                                      "re-approve the epic.")
        elif blockers:
            move = _move("blocked", "Blocked by " + ", ".join(blockers[:2]) + (f" +{len(blockers) - 2}" if len(blockers) > 2 else ""),
                         who="nobody", role="warn")
        elif row is not None:
            age = _span((self.now - row.last_at).total_seconds() / 60) if row.last_at else ""
            stale = row.status == "stale"
            move = _move("stale" if stale else "working",
                         f"{'Stale' if stale else 'Working'} · {row.harness or 'agent'}" + (f" {age}" if age else ""),
                         who="agent", role="warn" if stale else "info", since=row.last_at)
        else:
            move = _move("ready", "Ready", who="agent", role="neu", since=when(meta.get("updated")))
        agent = ({"harness": row.harness or "agent", "status": row.status, "since": row.claimed_at, "last": row.last_at,
                  "last_action": self._last_action(tid)} if row is not None else None)
        card = dict(self._static(t, own, tasks, entry))
        rollup = None
        if meta.get("type") == "epic":
            from orch.core.epics import rollup as epic_rollup
            rollup = epic_rollup(self.ws, t, self.entries, self.needs)
            if not own and status != "done":
                left = rollup["total"] - rollup["done"]
                move = (_move("ready", "No children yet", who="nobody", role="neu") if not rollup["total"]
                        else _move("working", f"{rollup['done']}/{rollup['total']} children done"
                                   + (f" · {rollup['testing']} in testing" if rollup["testing"] and left else ""),
                                   who="agent", role="info", since=when(meta.get("updated"))))
        from orch.dashboard.data.epic import epic_of
        epic = epic_of(self.ws, meta, self.epics)
        if epic is not None and card.get("parent") and card["parent"].upper() == epic["id"].upper():
            card["parent"] = None  # shown as the epic link instead
        from orch.core.query import idle_days
        card["idle_days"] = idle_days(self.ws, meta, status, self.now)  # outside the static cache: it ages by itself
        card.update(move=move, code=_code(t, self._reviews_of(tid)), agent=agent, blockers=blockers,
                    mentions=_mentioned(self._mentions.get(tid, ())), epic=epic, rollup=rollup)
        return _card(card)

    def _static(self, t, own, tasks, entry) -> dict:
        """The parts of a card that only the file (and its needs) decide: fields, gates, tasks, criteria, rule text.
        Kept across requests while the file and its needs are unchanged, like the parsed-ticket cache (a file
        written in the last few seconds is never kept)."""
        key = None
        if entry is not None:
            try:
                st = store.stat(entry.path)
            except OSError:
                st = None
            if st is not None and time.time_ns() - max(st.st_mtime_ns, st.st_ctime_ns) >= store.UNSTABLE_NS:
                key = (str(entry.path), st.st_mtime_ns, st.st_size, st.st_ino, st.st_ctime_ns, self.skip,
                       tuple((str(i.get("kind")), str(i.get("detail") or "")) for i in own))
                hit = _STATIC.get(key)
                if hit is not None:
                    _STATIC.move_to_end(key)
                    return hit
        meta, status = t.meta, t.status
        proven, total = evidence.progress(t)
        static = {
            "id": t.id, "title": t.title, "status": status, "status_label": STATUS_LABELS.get(status, status),
            "size": str(meta.get("size") or ""), "priority": str(meta.get("priority") or ""),
            "hot": meta.get("priority") in ("high", "urgent"), "type": str(meta.get("type") or ""),
            "external": _first_external(meta),
            "externals": [str(x["key"]) for x in _list(meta.get("external")) if isinstance(x, dict) and x.get("key") not in (None, "")],
            "parent": str(meta["parent"]) if isinstance(meta.get("parent"), (str, int)) and str(meta.get("parent")) else None,
            "labels": [x for x in _list(meta.get("labels")) if isinstance(x, str)],
            "updated": str(meta.get("updated") or ""), "updated_at": when(meta.get("updated")),
            "gates": _gates(t, own, self.skip), "tasks": _tasks(tasks) if status != "done" else None,
            "ac": {"proven": proven, "total": total}, "broken": False,
            "artifacts": len(artifact_entries(t)),
        }
        static["left"] = _left(t, static)
        if key is not None:
            _STATIC[key] = static
            while len(_STATIC) > STATIC_MAX:
                _STATIC.popitem(last=False)
        return static


def move_summary(card: dict) -> dict:
    """A card's move for machine readers (`orch show --json`, `orch list --json`, the ticket document): who
    (you|agent|nobody), kind (the move's `what`), label, ref, and why (when it is yours) and epic (a re-approval that
    is the epic's)."""
    m = card["move"]
    out = {"who": m["who"], "kind": m["what"], "label": m["label"], "ref": m.get("ref")}
    for key in ("why", "epic"):
        if m.get(key):
            out[key] = m[key]
    return out


def ticket_moves(ws, entries=None) -> dict[str, dict]:
    """{ticket id: move_summary} for every scanned ticket, from one shared `Cards`."""
    builder = Cards(ws, entries=entries)
    out = {}
    for e in builder.entries:
        card = builder.for_entry(e)
        if card is not None:
            out[e.id] = move_summary(card)
    return out


def ticket_card(ws, t, **kw) -> dict:
    """The card of one ticket (a fresh `Cards`; build one `Cards` per request when drawing several)."""
    return Cards(ws, **kw).for_ticket(t)
