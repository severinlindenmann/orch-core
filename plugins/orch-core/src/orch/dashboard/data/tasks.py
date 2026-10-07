"""Task lists in Mission Control (TasklistV3): the ticket page card, the progress bar, the Board
card and Today "In flight" summaries, and the Activity text. Pure functions over
tasks_view.view() output (or a scanned ticket file for the Board)."""
from __future__ import annotations

import re

from orch.core import query
from orch.core import tasks as tk
from orch.core.model import parse_body
from orch.dashboard.data.text import clean
from orch.errors import TicketParseError

_PLAN_CHECK = re.compile(r"^\s*[-*+]\s+\[[ xX]\]", re.M)
GLYPH = {"done": "✓", "doing": "◐", "todo": "○", "skipped": "–", "blocked": "▲"}
STATE_WORDS = {"done": "done", "doing": "in progress", "todo": "to do", "skipped": "skipped", "blocked": "blocked"}
ROLE_ICON = {"ok": "✓", "info": "◐", "you": "●", "warn": "▲", "err": "✕", "neu": "○"}
REF_ICON = {"file": "▤", "static": "▤", "artifact": "▢", "ticket": "○", "ext": "↗", "url": "↗",
            "section": "§", "ac": "◇", "q": "●"}
SEGMENT_LIMIT = 12
_BAR_ORDER = ("done", "skipped", "doing", "blocked", "todo")


def progress(summary: dict) -> dict:
    parts = [f"{summary['done']} of {summary['total']} done"]
    if summary["skipped"]:
        parts.append(f"{summary['skipped']} skipped")
    if summary["blocked"]:
        parts.append(f"{summary['blocked']} blocked")
    words = [f"{summary[s]} {STATE_WORDS[s]}" for s in _BAR_ORDER if summary[s]]
    return {"text": " · ".join(parts), "short": f"{summary['closed']}/{summary['total']}",
            "aria": f"{summary['closed']} of {summary['total']} tasks closed: " + ", ".join(words)}


def bar(summary: dict) -> list[tuple[str, int]]:
    return [(s, summary[s]) for s in _BAR_ORDER if summary[s]]


def _segments(states: list[str]) -> list[str] | None:
    return states if len(states) <= SEGMENT_LIMIT else None


def _chip(role: str, text: str, url: str | None = None) -> dict:
    return {"role": role, "text": text, "url": url, "icon": ROLE_ICON[role]}


def _state_chip(t: dict) -> dict | None:
    s, on = t["state"], t.get("on_ref") or {}
    if s == "doing":
        return _chip("info", "Now")
    if s == "skipped":
        return _chip("neu", "Skipped")
    if s == "blocked":
        if t["waits_on_you"]:
            return _chip("you", f"Needs you · {t['on']}", f"#q-{t['on']}" if on.get("kind") == "question" else f"#task-{t['on']}")
        if on.get("kind") == "ticket":
            return _chip("warn", f"Blocked by {on['id']}" + (f" ({on['status']})" if on.get("status") else ""), f"/t/{on['id']}")
        if t["on"]:
            return _chip("warn", f"Blocked by {t['on']}", on.get("url"))
        return _chip("warn", "Blocked")
    if s == "todo" and t["owner"] == "human" and not t["needs_open"]:
        return _chip("you", "Your task")
    return None


def _ref_chip(r: dict) -> dict:
    k = r["kind"]
    url = None
    if k == "file":
        text = (f"{r['repo']} · {r['path']}" if r.get("repo") else r["path"]) + (f"#L{r['lines']}" if r.get("lines") else "")
    elif k == "static":
        text = f"static · {r['path']}"
    elif k == "artifact":
        text, url = f"artifact · {r['target']}", r.get("url")
    elif k == "ticket":
        local = r.get("resolved")
        text = r["target"] + (f" ({local})" if local and local != r["target"] else "") + (f" · {r['status']}" if r.get("status") else "")
        url = f"/t/{local}" if local and r["exists"] else None
    elif k == "ext":
        text, url = r["target"], r.get("url")
    elif k == "url":
        text, url = r.get("label") or r["target"], r["target"]
    elif k == "ac":
        text = f"AC {r['target']}" + (f" · {r['text']}" if r.get("text") else "")
    elif k == "q":
        text, url = r["target"], f"#q-{r['target']}"
    else:  # section
        text = r["target"]
    if r.get("label") and k != "url":
        text += f" — {r['label']}"
    # the chip shows "AC2"; the criterion's text is its tooltip (it is printed in full in Proven)
    short = f"AC{r['target']}" if k == "ac" else text
    return {"kind": k, "icon": REF_ICON[k], "text": text, "short": short, "url": url, "external": k in ("ext", "url"),
            "missing": not r.get("exists", True)}


def _actions(t: dict, can_edit: bool) -> list[dict]:
    if not can_edit:
        return []
    s, out = t["state"], []
    if t["owner"] == "human" and s in ("todo", "doing") and not t["needs_open"]:
        out.append({"action": "done", "label": "Mark done", "message": "What proved it?" if t["verify"] else None,
                    "required": bool(t["verify"])})
    if s in ("todo", "doing", "blocked"):
        out.append({"action": "skip", "label": "Skip", "message": "Why skip it?", "required": True})
    if s in ("done", "skipped", "blocked"):
        out.append({"action": "reopen", "label": "Reopen", "message": None, "required": False})
    return out


def card(view: dict, *, can_edit: bool, plan_text: str = "") -> dict:
    rows = [{**t, "glyph": GLYPH[t["state"]], "word": STATE_WORDS[t["state"]],
             "closed": t["state"] in ("done", "skipped"), "chip": _state_chip(t),
             "refs": [_ref_chip(r) for r in t["refs"]], "actions": _actions(t, can_edit)}
            for t in view["tasks"]]
    return {"rows": rows, "progress": progress(view["summary"]), "segments": _segments([t["state"] for t in view["tasks"]]),
            "bar": bar(view["summary"]), "open": view["open"], "added_since": view["added_since_approval"],
            # every task came after the plan approval: said once above the list instead of a chip on each row
            "all_added_since": bool(rows) and view["added_since_approval"] == len(rows),
            "plan_note": bool(rows) and bool(_PLAN_CHECK.search(plan_text or "")),
            "can_edit": can_edit, "error": view.get("error")}


def _blocked_text(t: dict) -> str:
    return f"{t['id']} blocked by {t['on']}" if t.get("on") else f"{t['id']} blocked"


def flight(view: dict) -> dict | None:
    if view.get("error") or not view["tasks"]:
        return None
    by_id = {t["id"]: t for t in view["tasks"]}
    cur = by_id.get(view["doing"] or view["next"] or "")
    blocked = next((t for t in view["tasks"] if t["state"] == "blocked"), None)
    return {"now": {"id": cur["id"], "text": cur["text"]} if cur else None,
            "label": "Now" if view["doing"] else "Next", "progress": progress(view["summary"]),
            "segments": _segments([t["state"] for t in view["tasks"]]), "bar": bar(view["summary"]),
            "blocked": _blocked_text(blocked) if blocked else None}


# entry_tasks() results per ticket file, reused while the file's (mtime_ns, size) is unchanged
# (like query._NEEDS_CACHE): the Board and Today would otherwise read every body per request.
_TASKS_CACHE: dict[str, tuple[tuple[int, int], list[tk.Task] | None]] = {}


def entry_tasks(entry) -> list[tk.Task] | None:
    """The tasks of a scanned ticket file; None when the file or its Tasks section does not parse.
    Cached by (path, mtime_ns, size); callers must not mutate the returned tasks."""
    from orch.core import store
    try:
        st = store.stat(entry.path)  # once per request scope (the scan already stat'ed it)
    except OSError:
        return None
    key, fp = str(entry.path), (st.st_mtime_ns, st.st_size)
    hit = _TASKS_CACHE.get(key)
    if hit and hit[0] == fp:
        return hit[1]
    try:
        sections, _ = parse_body(entry.path.read_text(encoding="utf-8"))
        items = tk.parse(sections.get("Tasks", ""))
    except (OSError, UnicodeDecodeError, TicketParseError):
        items = None
    _TASKS_CACHE[key] = (fp, items)
    return items


def waiting_on_human(needs, entries=None) -> set[str]:
    """Upper-case ids of the tickets whose needs-you items stop the agent: those with a blocking item (core decides,
    query._needs_you: a ready human task counts only once the agent has no doing or next task left; a non-blocking
    question never counts). `entries` is unused and kept for callers."""
    return query.blocking_ids(needs)


def board_progress(entry) -> dict | None:
    items = entry_tasks(entry)
    if not items:
        return None
    s = tk.summary(items)
    return {"progress": progress(s), "segments": _segments([t.state for t in items]), "bar": bar(s), "blocked": s["blocked"]}


def _short(text) -> str:
    text = clean(text)
    return text if len(text) <= 80 else text[:80]


def describe(event) -> str | None:
    d, kind = event.data or {}, event.kind
    if kind == "task.added":
        ids = ", ".join(d.get("tasks") or [])
        if d.get("imported"):
            return f"imported {ids} from the Plan"
        return f"added {ids}" + (" after plan approval" if d.get("after_approval") else "")
    if kind == "task.edited":
        return f"edited {d.get('task', 'a task')}"
    if kind == "task.moved":
        tid, now = d.get("task", "a task"), d.get("now")
        if now == "doing":
            return f"started {tid}"
        if now == "done":
            return f"finished {tid}" + (f": {_short(d['note'])}" if d.get("note") else "")
        if now == "skipped":
            return f"skipped {tid}: {_short(d.get('why', ''))}"
        if now == "blocked":
            return f"blocked {tid}" + (f" on {d['on']}" if d.get("on") else "") + (f": {_short(d['why'])}" if d.get("why") else "")
        if now == "todo":
            return f"reopened {tid}"
    return None
