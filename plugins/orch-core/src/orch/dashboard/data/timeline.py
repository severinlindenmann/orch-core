from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta

from orch.clock import now as clock_now
from orch.clock import parse_stamp
from orch.core import events as events_mod
from orch.dashboard.data.text import clean as _clean
from orch.dashboard.data.text import md_escape

CATEGORIES = ("decisions", "questions", "code", "agents", "checks", "ledger")
CATEGORY_LABELS = {"decisions": "Decisions", "questions": "Questions", "code": "Code", "agents": "Agents", "checks": "Checks",
                   "ledger": "Ledger"}
HIDDEN_FROM_ALL = ("ledger",)  # bookkeeping: `orch ledger adopt` writes one event per ticket; only its own filter shows it

# Addons whose actions are about code (PRs, issues): they file under Code; every other addon action stays uncategorised.
_CODE_ADDONS = ("github-issues", "github-reviews")
_TICKET_KEY = re.compile(r"[A-Za-z][A-Za-z0-9]*-\d{1,6}")

_LINK_KEYS = ("branches", "prs", "worktrees", "branch", "pr", "worktree")


def category(event) -> str | None:
    if event.via == "check":
        return "checks"
    if event.kind.startswith("gate.") or event.kind == "verdict.given":
        return "decisions"
    if event.kind.startswith("question."):
        return "questions"
    if event.kind == "artifact.added":
        return "code"
    if event.kind == "ledger.adopted":
        return "ledger"
    if event.kind == "addon.decision":
        return "decisions"
    if event.kind == "addon.action" and (event.data or {}).get("addon") in _CODE_ADDONS:
        return "code"
    if event.kind in ("state.updated", "ticket.edited") and any(k in event.data for k in _LINK_KEYS):
        return "code"
    if event.kind in ("claim.taken", "claim.released", "ticket.created"):
        return "agents"
    if event.kind.startswith("task."):
        return "agents"
    return None


_category_of = category  # alias so `timeline`'s `category` parameter doesn't shadow this function


def _shorten(text, limit: int = 80) -> str:
    text = _clean(text)
    return text if len(text) <= limit else text[:limit]


def _pr_label(pr: str) -> str:
    m = re.search(r"(\d+)/?$", str(pr))
    return f"PR #{m.group(1)}" if m else "a PR"


def _describe_link(data: dict) -> str:
    parts = []
    if data.get("branch"):
        parts.append(f"branch {_shorten(data['branch'], 60)}")
    if data.get("worktree"):
        parts.append(f"worktree {_shorten(data['worktree'], 60)}")
    if data.get("pr"):
        parts.append(f"{_pr_label(data['pr'])} / {_shorten(data['pr'], 60)}")
    if data.get("external"):
        parts.append(_shorten(data["external"], 60))
    return "linked " + ", ".join(parts) if parts else "edited the ticket"


def _words(raw, fallback: str) -> str:
    """An id like "mark_ready" or "github-reviews" as words ("mark ready", "github reviews")."""
    text = _shorten(raw, 60).replace("_", " ").replace("-", " ").strip()
    return text or fallback


def _describe_addon_action(data: dict) -> str:
    """An addon action as a sentence: `mark ready on DEMO-0010 (github reviews)`, not the addon's internal target
    ("DEMO-0010|GH-14|done"). The addon's own ids are only turned into words."""
    target = str(data.get("target") or "")
    keys = list(dict.fromkeys(_TICKET_KEY.findall(target)))
    on = f" on {', '.join(keys[:2])}" if keys else ""
    return f"{_words(data.get('action'), 'ran an action')}{on} ({_words(data.get('addon'), 'an addon')})"


def _ticket_of(event) -> str | None:
    """The ticket an event is about: its own, else (an addon action has none) the first key in the action's target."""
    if event.ticket:
        return event.ticket
    if event.kind == "addon.action":
        m = _TICKET_KEY.search(str((event.data or {}).get("target") or ""))
        return m.group(0) if m else None
    return None


def describe(event) -> str:
    data = event.data or {}
    kind = event.kind
    if kind.startswith("task."):
        from orch.dashboard.data.tasks import describe as describe_task
        return describe_task(event) or kind.replace(".", " ")
    if kind == "ticket.created":
        return "created the ticket"
    if kind == "ticket.moved":
        if data.get("command") in ("close", "reopen"):
            verb = "closed" if data["command"] == "close" else "reopened"
            how = (f" as {data['resolution']}" + (f" by {data['superseded_by']}" if data.get("superseded_by") else "")
                   if data.get("resolution") else "")
            return f"{verb} the ticket{how} ({data.get('from', '?')} → {data.get('to', '?')}): {data.get('reason', '')}"
        return f"moved {data.get('from', '?')} → {data.get('to', '?')}"
    if kind == "gate.approved":
        return f"approved the {data.get('gate', 'gate')}"
    if kind == "gate.invalidated":
        return f"invalidated the {data.get('gate', 'gate')}"
    if kind == "gate.changes_requested":
        return f"asked for changes on the {data.get('gate', 'gate')}: {data.get('message', '')}"
    if kind == "verdict.given":
        return f"gave verdict {data.get('verdict', '?')}"
    if kind == "question.asked":
        qids = data.get("qids") or []
        blocked = f" · {data['task_blocked']} blocked" if data.get("task_blocked") else ""
        return ("asked " + ", ".join(qids) + blocked) if qids else ("asked a question" + blocked)
    if kind == "question.answered":
        qid = data.get("qid", "")
        restarted = f" · {data['task_restarted']} restarted" if data.get("task_restarted") else ""
        return (f"answered {qid}" + restarted) if qid else ("answered a question" + restarted)
    if kind == "claim.taken":
        return "claimed the ticket"
    if kind == "claim.released":
        return "released the claim"
    if kind == "artifact.added":
        return f"added artifact {_shorten(data.get('name', ''))}"
    if kind == "state.updated":
        return "updated state"
    if kind == "ticket.edited":
        section = data.get("section")
        if section:
            return f"edited {section}"
        if any(data.get(k) for k in ("branch", "worktree", "pr", "external")):
            return _describe_link(data)
        if data.get("labels_added") or data.get("labels_removed"):
            return "changed the labels"
        return "edited the ticket"
    if kind == "log.added":
        return f"logged: {_shorten(data.get('text', ''))}"
    if kind == "ledger.adopted":
        return "adopted an earlier decision into the ledger"
    if kind == "addon.action":
        return _describe_addon_action(data)
    if kind == "addon.decision":
        return f"chose {_words(data.get('choice'), 'an option')} on an item of {_words(data.get('addon'), 'an addon')}"
    if kind.startswith("quick.") and kind in _QUICK:
        q = str(data.get("quick") or "a quick task")
        return _QUICK[kind].format(q=q, ticket=str(data.get("ticket") or "a ticket"))
    return kind.replace(".", " ")


# Quick tasks (orch.core.quick): fixed phrases, never the task's own text
_QUICK = {"quick.added": "added quick task {q}", "quick.claimed": "claimed quick task {q}",
          "quick.released": "released quick task {q}", "quick.done": "finished quick task {q}",
          "quick.outgrew": "stopped quick task {q}: it outgrew the size limit", "quick.reopened": "reopened quick task {q}",
          "quick.dropped": "dropped quick task {q}", "quick.promoted": "made quick task {q} the ticket {ticket}",
          "quick.artifact": "added a file to quick task {q}"}


_PHRASE = {"ticket.created": "created the ticket", "claim.taken": "claimed the ticket", "claim.released": "released the claim",
           "log.added": "added a log line", "state.updated": "updated the handoff", "artifact.added": "added a file",
           "question.asked": "asked a question", "question.answered": "answered a question",
           "gate.approved": "approved a gate", "gate.changes_requested": "asked for changes",
           "gate.invalidated": "changed a gate's text", "verdict.given": "gave a verdict",
           "task.added": "added tasks", "task.edited": "edited a task"}
_TASK_VERB = {"doing": "started", "done": "finished", "skipped": "skipped", "blocked": "blocked", "todo": "reopened"}
_SAFE_ID = re.compile(r"^[A-Za-z]{1,3}\d{1,4}$")


def action_phrase(event) -> str:
    """A fixed phrase for an event, for status slots (cards, headers, Today rows): never any text an agent or
    a tracker wrote (log lines, notes, reasons, branch names). `describe()` is for the attributed timeline."""
    d, kind = event.data or {}, event.kind
    if kind == "ticket.moved":
        to = str(d.get("to") or "")
        return {"close": "closed the ticket", "reopen": "reopened the ticket"}.get(str(d.get("command")), f"moved it to {to}"
                                                                                  if to in ("backlog", "open", "in-progress", "waiting", "testing", "done") else "moved it")
    if kind == "task.moved":
        tid = str(d.get("task") or "")
        verb = _TASK_VERB.get(str(d.get("now")), "moved")
        return f"{verb} {tid}" if _SAFE_ID.match(tid) else f"{verb} a task"
    if kind == "ticket.edited":
        if any(d.get(k) for k in ("branch", "pr", "worktree", "external")):
            return "linked code" if not d.get("external") else "linked an issue"
        if d.get("labels_added") or d.get("labels_removed"):
            return "changed the labels"
        from orch.core.constants import FILE_ORDER
        return f"edited {d['section']}" if d.get("section") in FILE_ORDER else "edited the ticket"
    return _PHRASE.get(kind, "updated the ticket")


def who(event) -> str:
    actor = event.actor
    if actor.startswith("human:"):
        return "you"
    if actor.startswith("agent:"):
        return actor[len("agent:"):].split(":", 1)[0]
    return actor


@dataclass
class TimelinePage:
    groups: list[dict]
    older: int | None


PAGE_SIZE = 50  # Activity shows this many events per page; "Older" pages on
RUN_MIN = 3  # this many events in a row by the same actor of the same kind fold into one line


_RUN_PHRASE = {"ticket.edited": "edited the ticket", "ticket.moved": "moved tickets", "task.moved": "moved tasks"}


def _run_phrase(kind: str) -> str:
    """What a run of different events of one kind did, in a fixed phrase (never agent-written text)."""
    return _PHRASE.get(kind) or _RUN_PHRASE.get(kind, "updated the ticket")


def _runs(items: list[dict]) -> list[dict]:
    """A day's items as entries: an item on its own, or a run of RUN_MIN+ consecutive items by the same actor and
    of the same kind ("claude-code claimed the ticket · 8 times") holding its items for a disclosure."""
    out: list[dict] = []
    i = 0
    while i < len(items):
        j = i
        while j + 1 < len(items) and (items[j + 1]["who"], items[j + 1]["kind"]) == (items[i]["who"], items[i]["kind"]):
            j += 1
        chunk = items[i:j + 1]
        if len(chunk) >= RUN_MIN:
            whats = {c["what"] for c in chunk}
            tickets = list(dict.fromkeys(c["ticket"] for c in chunk if c["ticket"]))
            out.append({"run": True, "count": len(chunk), "time": chunk[0]["time"], "category": chunk[0]["category"],
                        "who": chunk[0]["who"], "what": whats.pop() if len(whats) == 1 else _run_phrase(chunk[0]["kind"]),
                        "tickets": tickets, "items": chunk})
        else:
            out.extend({"run": False, **c} for c in chunk)
        i = j + 1
    return out


def _parse(stamp: str | None) -> datetime | None:
    if not stamp:
        return None
    try:
        return parse_stamp(str(stamp))
    except ValueError:
        return None


def _day_label(day, today, yesterday) -> str:
    if day == today:
        return "Today"
    if day == yesterday:
        return "Yesterday"
    return day.strftime("%a %d.%m.")


def timeline(ws, *, category: str = "all", before: int | None = None, limit: int = 200,
             now: datetime | None = None) -> TimelinePage:
    at_now = now or clock_now()
    all_events = events_mod.read_events(ws)
    all_events.sort(key=lambda ev: ev.seq, reverse=True)

    candidates = all_events
    if before is not None:
        candidates = [ev for ev in candidates if ev.seq < before]
    if category != "all":
        candidates = [ev for ev in candidates if _category_of(ev) == category]
    else:
        candidates = [ev for ev in candidates if _category_of(ev) not in HIDDEN_FROM_ALL]

    shown = candidates[:limit]
    older = shown[-1].seq if len(candidates) > limit and shown else None

    today = at_now.astimezone().date()
    yesterday = today - timedelta(days=1)

    groups: list[dict] = []
    for ev in shown:
        at = _parse(ev.at)
        local_at = at.astimezone() if at is not None else at_now.astimezone()
        day = local_at.date()
        label = _day_label(day, today, yesterday)
        if not groups or groups[-1]["label"] != label:
            groups.append({"label": label, "items": []})
        groups[-1]["items"].append({
            "seq": ev.seq,
            "time": local_at,
            "category": _category_of(ev),
            "who": who(ev),
            "what": describe(ev),
            "kind": ev.kind,
            "ticket": _ticket_of(ev),
        })
    for g in groups:
        g["entries"] = _runs(g["items"])

    return TimelinePage(groups=groups, older=older)


def to_markdown(page: TimelinePage) -> str:
    lines = []
    for group in page.groups:
        lines.append(f"## {md_escape(group['label'])}")
        for item in group["items"]:
            time_s = item["time"].strftime("%H:%M")
            bits = [md_escape(time_s), md_escape(item["who"]), md_escape(item["what"])]
            if item["ticket"]:
                bits.append(md_escape(item["ticket"]))
            lines.append("- " + " · ".join(bits))
    return "\n".join(lines) + ("\n" if lines else "")


# -- R21: what a paired phone applied, as receipts ----------------------------------------------------------------

RECEIPT_HOURS = 24
RECEIPT_ROWS = 5
_PHONE_LABEL = re.compile(r"^phone:([A-Za-z0-9 ._-]{1,40})$")


def via_label(via) -> str:
    """How a decision's `via` reads on the page: "from your phone (iPhone)" for a paired phone, else as stored."""
    m = _PHONE_LABEL.match(str(via or ""))
    return f"from your phone ({m.group(1)})" if m else str(via or "")


def _receipt_text(event) -> str | None:
    d, tid = event.data or {}, event.ticket
    gate = d.get("gate") if d.get("gate") in ("requirements", "plan") else None
    if event.kind == "gate.approved":
        return f"Approved {gate or 'a gate'} of {tid}"
    if event.kind == "gate.changes_requested":
        return f"Asked for changes on the {gate or 'gate'} of {tid}"
    if event.kind == "question.answered":
        qid = str(d.get("qid") or "")
        return f"Answered {qid if re.fullmatch(r'Q[1-9][0-9]*', qid) else 'a question'} on {tid}"
    if event.kind == "verdict.given":
        verdict = d.get("verdict") if d.get("verdict") in ("done", "follow-up") else None
        return f"Verdict {verdict} on {tid}" if verdict else f"Gave the verdict on {tid}"
    if event.kind == "ticket.created":
        return f"Created {tid}"
    return None


def phone_receipts(ws, events, now=None, hours: int = RECEIPT_HOURS, limit: int = RECEIPT_ROWS) -> list[dict]:
    """The decisions a paired phone applied in the last `hours`, newest first: {ticket, text, via, at, unverified}.
    Only events the remote ledger says a verified phone decision wrote, so a forged `phone:` line alone never shows;
    `unverified` when the signed approval ledger does not back it too (ledger.phone_event_signed);
    the text is a fixed phrase, never the decision's free text."""
    phone = [e for e in events if str(e.via).startswith("phone:")]
    if not phone:
        return []
    from orch.core import ledger as signed_ledger
    from orch.remote import ledger as remote_ledger
    seqs, signed = remote_ledger.applied_seqs(ws), signed_ledger.entries(ws)
    since = (now or clock_now()) - timedelta(hours=hours)
    out = []
    for e in reversed(phone):
        try:
            at = parse_stamp(e.at)
        except (TypeError, ValueError):
            continue
        if e.seq not in seqs or at < since:
            continue
        text = _receipt_text(e)
        if text:
            # the remote ledger is agent-writable: a decision kind the signed ledger does not back reads "unverified"
            out.append({"ticket": e.ticket, "text": text, "via": via_label(e.via), "at": at,
                        "unverified": signed_ledger.phone_event_signed(e, signed) is False})
        if len(out) >= limit:
            break
    return out
