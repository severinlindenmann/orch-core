"""Text for what the read operations show. Everything derived from a ticket (titles, section text, criteria, tasks,
questions, notes, artifact names) goes through :func:`orch.cli.render.fence`; what stays outside a fence is a key,
an id, a status or another token from a closed list, or a number.
"""

from __future__ import annotations

from typing import Any

from orch.cli.render import fence
from orch.ops.runtime import short

PRIORITY = {"urgent": 0, "high": 1, "medium": 2, "low": 3}
FIELDS_SHOWN = ("title", "priority", "size", "labels", "due", "parent", "blocked_by", "acceptance", "tasks")
ALL_FIELDS = (*FIELDS_SHOWN, "links")
MORE = 8  # criteria and tasks listed in the default view before "+N more"


def key_number(key: str) -> int:
    return int(key.rpartition("-")[2])


def open_questions(view: Any) -> list[Any]:
    return [q for q in view.questions if not q.answered]


def next_task(view: Any, session: str | None = None) -> Any | None:
    """The task to do next: one this session leases first, then the first open one nobody else leases."""
    mine = [t for t in view.tasks if t.state == "started" and t.leased_by is not None and t.leased_by == session]
    if mine:
        return mine[0]
    free = [t for t in view.tasks if t.state == "open" and (t.leased_by is None or t.leased_by == session)]
    return free[0] if free else None


def next_hint(view: Any, session: str | None = None) -> str:
    """The ``next:`` line after a write: ids and commands only, never ticket text."""
    if any(q.blocking for q in open_questions(view)):
        return "orch wait"
    t = next_task(view, session)
    if t is not None:
        return f"orch task start {t.id}"
    if view.tasks and all(x.state in ("done", "skipped") for x in view.tasks):
        return "orch submit"
    return "orch show"


def task_line(t: Any, n: int = 70) -> str:
    return f"{t.id} {t.state}{' (proves ' + ','.join(t.proves) + ')' if t.proves else ''}: {short(t.text, n)}"


def ac_line(a: Any, n: int = 70) -> str:
    return f"{a.id} {'evidence' if a.evidence else 'no evidence'}: {short(a.text, n)}"


def question_line(q: Any, texts: dict[str, Any]) -> str:
    return f"{q.id} {'blocking' if q.blocking else 'open'} to={q.to}: {short(texts.get(q.id, ''), 80)}"


def fenced_tasks(view: Any, label: str, n: int = 70) -> list[str]:
    return fence("\n".join(task_line(t, n) for t in view.tasks) or "no tasks", label)


def event_line(e: dict[str, Any]) -> str:
    """One event as ``#seq type by detail`` (detail: a task, a question, an artifact name or the start of a note)."""
    a = e["actor"]
    if a["kind"] == "agent":
        who = f"{a['id']}:{a['session'][:10]}" + ("(unattended)" if a.get("unattended") else "")
    elif a["kind"] == "person":
        who = a["id"][:10]
    else:
        who = "host"
    detail = ""
    t = e["type"]
    if t.startswith("task."):
        detail = e["task"]
    elif t.startswith("artifact."):
        detail = e["name"]
    elif t == "log.added":
        detail = short(e["text"], 50)
    elif t == "question.asked":
        detail = e["question"]["id"]
    elif t in ("gate.approved", "gate.changes_requested"):
        detail = e["gate"]
    elif t == "verdict.given":
        detail = e["outcome"]
    return f"#{e['seq']} {t} {who}" + (f" {detail}" if detail else "")


def next_ticket(c: Any, *, mine_first: bool = True) -> Any | None:
    """The ticket to work on: the session's own claim (``mine_first``), else the best free one the actor may see.

    Free means ``open`` with no live claim and nothing it is blocked by still undone; the best is the highest
    priority, then the lowest number."""
    if mine_first:
        mine = c.mine()
        if mine:
            return mine[0]
    c.store.load_all()
    tickets = c.store.state.tickets.values()
    status = {v.key: v.status for v in tickets}
    free = [
        v
        for v in tickets
        if c.sees(v)
        and v.status == "open"
        and not (v.claim is not None and v.claim.live)
        and all(status.get(k, "done") in ("done", "closed") for k in v.fields["blocked_by"])
    ]
    free.sort(key=lambda v: (PRIORITY.get(v.fields["priority"], 2), key_number(v.key)))
    return free[0] if free else None
