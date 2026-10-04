"""Board · External and the ticket's GitHub issue panel (spec v2 §13.2). Reads the cache through `view` and never runs
commands. Linked tickets and out-of-sync state come from the core (view.links())."""
from __future__ import annotations

import re
from datetime import datetime

from orch.addons.widgets import KV, Action, Badge, Callout, Card, Chips, Link, Table, Text
from orch.clock import now

from .ignore import ignored, token
from .provider import github_trackers

FILTERS = (("mine", "Mine"), ("sprint", "Current sprint"), ("all", "All open"))
_FILTER_LABEL = dict(FILTERS)
CATEGORY = {"todo": ("neu", "to do"), "in_progress": ("info", "in progress"), "done": ("ok", "done")}
COLUMNS = ("Key", "Title", "Status", "Priority", "Assignee", "Sprint", "Local ticket")
SYNC_COLUMNS = ("Key", "GitHub", "Local ticket", "Fix", "Ignore")
BEHIND = 10  # percentage points behind "expected by today" before a sprint shows "behind plan"


def _str(value) -> str:
    return value if isinstance(value, str) else ""


def _items(view) -> list[dict]:
    out = []
    for snap in view.snapshots("issues"):
        out.extend(i for i in snap.items if isinstance(i, dict) and isinstance(i.get("key"), str))
    return out


def _me(view):
    return next((s.me for s in view.snapshots("issues") if s.me), None)


def _sprint(item) -> dict | None:
    s = item.get("sprint")
    return s if isinstance(s, dict) else None


def _in(item, sprint) -> bool:
    return sprint is not None and (_sprint(item) or {}).get("number") == sprint.get("number")


def _when(value) -> datetime | None:
    try:
        dt = datetime.fromisoformat(value) if isinstance(value, str) else None
    except ValueError:
        return None
    return dt if dt is not None and dt.tzinfo is not None else None


def _rank(item) -> tuple:
    rank = item.get("rank") if isinstance(item.get("rank"), int) else 9
    number = item.get("number") if isinstance(item.get("number"), int) else 0
    return rank, number


def _status(item):
    return Badge(*CATEGORY.get(item.get("category"), ("neu", _str(item.get("category")) or "unknown")))


def _key_cell(item):
    url = item.get("url")
    return Link(item["key"], url) if isinstance(url, str) and url.startswith("https://") else item["key"]


def current_sprint(items) -> dict | None:
    return next((s for i in items if (s := _sprint(i)) and s.get("state") == "active"), None)


def progress(items, sprint, today) -> dict:
    members = [i for i in items if _in(i, sprint)]
    done = sum(1 for i in members if i.get("category") == "done")
    pct = round(100 * done / len(members)) if members else 0
    start, end = _when(sprint.get("start")), _when(sprint.get("end"))
    expected = None
    if start and end and end > start:
        expected = round(100 * min(1.0, max(0.0, (today - start) / (end - start))))
    return {"done": done, "total": len(members), "pct": pct, "expected": expected}


def out_of_sync(view, items, links) -> list:
    skip = ignored(view.state_dir)
    out = []
    for item in items:
        for ticket, action in links.sync(item["key"], item.get("category")):
            if token(ticket.id, item["key"], item.get("category")) not in skip:
                out.append((item, ticket, action))
    return out


_TASK_LINE = re.compile(r"^[-*+] \[([ /!xX-])\] T\d+", re.MULTILINE)
_OPEN_BOXES = frozenset({" ", "/", "!"})


def _open_task_count(ticket) -> int:
    text = ticket.section("Tasks") if ticket is not None else ""
    return sum(1 for m in _TASK_LINE.finditer(text) if m.group(1) in _OPEN_BOXES)


def _open_question_count(ticket) -> int:
    qs = (ticket.meta.get("questions") if ticket is not None and isinstance(ticket.meta, dict) else None) or []
    return sum(1 for q in qs if isinstance(q, dict) and q.get("answer") in (None, ""))


def _close_confirm(ticket) -> str | None:
    """How many open tasks a close would skip and how many open questions would stay unanswered, read from the
    ticket at render time; None (the manifest's static text) when we have no ticket to compute from or nothing to warn about."""
    if ticket is None:
        return None
    tasks, questions = _open_task_count(ticket), _open_question_count(ticket)
    if not tasks and not questions:
        return None
    parts = []
    if tasks:
        parts.append(f"{tasks} open task{'s' if tasks != 1 else ''} will be skipped")
    if questions:
        parts.append(f"{questions} open question{'s' if questions != 1 else ''} will stay unanswered")
    return f"Close the local ticket? {' and '.join(parts)}."


def _fix(item, ticket, action, full_ticket=None) -> Action:
    # The target is the local ticket id: the close/reopen Intent's ref must equal it (core refuses any other ticket).
    if action == "close":
        return Action("close_local", "Close local", ticket.id, confirm=_close_confirm(full_ticket))
    return Action("reopen_local", "Reopen local", ticket.id)


def _local(item, links):
    tickets = links.for_external(item["key"])
    if not tickets:
        return Action("import", "Import", item["key"])
    if len(tickets) == 1:
        return Link(f"{tickets[0].id} · {tickets[0].status}", f"/t/{tickets[0].id}")
    return Text(", ".join(f"{t.id} · {t.status}" for t in tickets))


def _row(item, links) -> tuple:
    return (_key_cell(item), _str(item.get("title")), _status(item), _str(item.get("priority")) or None,
            _str(item.get("assignee")) or None, (_sprint(item) or {}).get("name"), _local(item, links))


def _filters(view, items, shown: str, sprint, me_unknown: bool) -> Card:
    open_items = [i for i in items if i.get("category") != "done"]
    counts = {"mine": "unknown" if me_unknown else sum(1 for i in open_items if i.get("assigned_to_me") is True),
              "sprint": sum(1 for i in items if _in(i, sprint)), "all": len(open_items)}
    chips = tuple(Link(f"{label} {counts[key]}", f"/board?view=external&filter={key}", current=key == shown)
                  for key, label in FILTERS)
    default = view.settings.get("default_filter") or "mine"
    note = Text(f"Default: {_FILTER_LABEL.get(default, 'Mine')}. Change it in Workspace & addons.")
    return Card("Show", (Chips(chips, label="Filter issues"), note, Link("Workspace & addons", "/workspace#addons")))


def _sprint_card(items, sprint) -> Card:
    p = progress(items, sprint, now())
    rows = [("Done", f"{p['done']} of {p['total']} ({p['pct']}%)"),
            ("Expected by today", f"{p['expected']}%" if p["expected"] is not None else "unknown (no start or due date)"),
            ("Ends", _str(sprint.get("end"))[:10] or "no due date")]
    behind = p["expected"] is not None and p["pct"] + BEHIND < p["expected"]
    if behind:
        rows.append(("Pace", Badge("warn", "behind plan")))
    return Card(f"Sprint · {sprint.get('name')}", (KV(tuple(rows)),), role="warn" if behind else None)


def _sync_card(view, items, links):
    rows = []
    for item, ticket, action in out_of_sync(view, items, links):
        there = Badge("ok", "closed in GitHub") if action == "close" else Badge("info", "open in GitHub")
        rows.append((_key_cell(item), there, Link(f"{ticket.id} · {ticket.status}", f"/t/{ticket.id}"), _fix(item, ticket, action),
                     Action("ignore", "Ignore", token(ticket.id, item["key"], item.get("category")))))
    if not rows:
        return None
    return Card("Out of sync", (Text("GitHub and the local ticket disagree. Fix the local ticket, or ignore the difference "
                                     "until the issue changes again."), Table(SYNC_COLUMNS, tuple(rows))), role="warn")


def board(view) -> list:
    if not github_trackers(view.trackers()):
        return [Callout("neu", "No GitHub tracker",
                        "Add one to external_trackers in orchestrator/config.json, e.g. prefix GH, pattern GH-(?P<id>\\d+), "
                        "url https://github.com/<owner>/<repo>/issues/{id}.")]
    items = _items(view)
    if not items:
        return [Callout("neu", "No GitHub issues yet", "They load in the background while Mission Control is open.")]
    links = view.links()
    me = _me(view)
    me_unknown = me is None and any(s.health != "never_fetched" for s in view.snapshots("issues"))
    sprint = current_sprint(items)
    wanted = view.params.get("filter") or view.settings.get("default_filter") or "mine"
    shown = wanted if wanted in _FILTER_LABEL else "mine"
    open_items = [i for i in items if i.get("category") != "done"]
    notes, title = [], _FILTER_LABEL[shown]
    if shown == "mine":
        rows = [i for i in open_items if i.get("assigned_to_me") is True]
        if not rows:
            rows = [i for i in open_items if _in(i, sprint) and not links.for_external(i["key"])]
            name = sprint.get("name") if sprint else "the current sprint"
            title = f"{name} · not imported"
            notes.append(Callout("info", "Nothing in GitHub is assigned to you" if me else "Who you are in GitHub is not known yet",
                                 f"Showing {title}."))
    elif shown == "sprint":
        rows = [i for i in items if _in(i, sprint)]
    else:
        rows = open_items
    rows.sort(key=_rank)
    out = [_filters(view, items, shown, sprint, me_unknown)]
    sync = _sync_card(view, items, links)
    if sync is not None:
        out.append(sync)
    out.extend(notes)
    if sprint is not None:
        out.append(_sprint_card(items, sprint))
    out.append(Card(f"GitHub issues · {title}", (Table(COLUMNS, tuple(_row(i, links) for i in rows),
                                                     empty="No issues match this filter."),)))
    return out


def ticket_panel(view) -> list:
    ticket = view.ticket
    if ticket is None:
        return []
    ours = list(github_trackers(view.trackers()).values())
    keys = [str(x["key"]).upper() for x in ticket.meta.get("external") or []
            if isinstance(x, dict) and isinstance(x.get("key"), (str, int)) and str(x["key"]).strip()]
    keys = [k for k in keys if any(t.matches(k) for t in ours)]
    if not keys:
        return []
    items = {i["key"].upper(): i for i in _items(view)}
    links = view.links()
    skip = ignored(view.state_dir)
    out = []
    for key in keys:
        item = items.get(key)
        if item is None:
            out.append(Card(f"{key} · GitHub issue", (Text("Not in the cached GitHub issues yet."),)))
            continue
        rows = (("Title", Link(_str(item.get("title")) or key, item["url"]) if _str(item.get("url")).startswith("https://") else _str(item.get("title"))),
                ("Status", _status(item)), ("Assignee", _str(item.get("assignee")) or None),
                ("Sprint", (_sprint(item) or {}).get("name")),
                ("Labels", ", ".join(x for x in item.get("labels") or [] if isinstance(x, str)) or None))
        body = [KV(rows)]
        hit = next((h for h in links.sync(key, item.get("category")) if h[0].id == ticket.id), None)
        if hit and token(ticket.id, key, item.get("category")) not in skip:
            body.append(Callout("warn", "Out of sync",
                                "Closed in GitHub, not done here." if hit[1] == "close" else "Open again in GitHub, done here."))
            body.append(KV((("Fix", _fix(item, hit[0], hit[1], full_ticket=ticket)),
                            ("Or", Action("ignore", "Ignore", token(ticket.id, key, item.get("category")))))))
        out.append(Card(f"{key} · GitHub issue", tuple(body)))
    return out
