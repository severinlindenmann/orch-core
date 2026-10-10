"""Text for what the read operations show. Everything derived from a ticket (titles, section text, criteria, tasks,
questions, notes, artifact names) goes through :func:`orch.cli.render.fence`; what stays outside a fence is a key,
an id, a status or another token from a closed list, or a number.
"""

from __future__ import annotations

from typing import Any

from orch.cli import render
from orch.ops import decisions
from orch.ops.runtime import short


def short_gate_hash(h: str | None) -> str:
    """The first 8 hex digits of a gate hash: what ``orch show`` prints per open gate and what the signing review
    prints last, so a person can compare the two (F1 5.7)."""
    return (h or "").removeprefix("sha256:")[:8] or "-"


def fence(text: str, label: str = "ticket") -> list[str]:
    """``render.fence`` for a handler result: the renderer escapes the content once, for every line."""
    return render.fence(text, label, raw=True)


PRIORITY = {"urgent": 0, "high": 1, "medium": 2, "low": 3}
FIELDS_SHOWN = ("title", "priority", "size", "labels", "due", "parent", "blocked_by", "acceptance", "tasks")
ALL_FIELDS = (*FIELDS_SHOWN, "links")
MORE = 6  # criteria and tasks listed in the default view before "+N more"


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


def open_gate(view: Any) -> str | None:
    """The first of ``requirements`` and ``plan`` that applies to the ticket and is not approved, if any."""
    for g in ("requirements", "plan"):
        v = view.gates[g]
        if v.applies and not v.approved:
            return g
    return None


def next_hint(view: Any, session: str | None = None) -> str:
    """The ``next:`` line after a write: ids and commands only, never ticket text."""
    if view.status == "testing":
        return "orch wait"  # submitted: a person decides now
    if view.status in ("done", "closed"):
        return "orch next"
    if any(q.blocking for q in open_questions(view)):
        return "orch wait"
    t = next_task(view, session)
    if t is not None:
        if t.state == "started":
            verify = next((x["verify"] for x in view.fields["tasks"] if x["id"] == t.id), None)
            return f"orch task done {t.id}" + (" --run" if verify else "")
        return f"orch task start {t.id}"
    if view.tasks and all(x.state in ("done", "skipped") for x in view.tasks):
        g = open_gate(view)
        return f'orch ask "approve the {g} gate?"' if g else "orch submit"
    return "orch show"


def task_line(t: Any, n: int = 48) -> str:
    return f"{t.id} {t.state}{' (' + ','.join(t.proves) + ')' if t.proves else ''}: {short(t.text, n)}"


def ac_line(a: Any, n: int = 48) -> str:
    return f"{a.id} [{'x' if a.evidence else ' '}] {short(a.text, n)}"


def question_line(q: Any, texts: dict[str, Any]) -> str:
    return f"{q.id} {'blocking' if q.blocking else 'open'} to={q.to}: {short(texts.get(q.id, {}).get('text', ''), 80)}"


def fenced_tasks(view: Any, label: str, n: int = 100) -> list[str]:
    return fence("\n".join(task_line(t, n) for t in view.tasks) or "no tasks", label)


def event_line(e: dict[str, Any]) -> str:
    """One event as ``#seq type by detail`` (detail: a task, a question, an artifact name or the start of a note);
    ``by`` is ``a`` for an agent (``u`` unattended), ``h`` for the host or the first characters of a person id."""
    a = e["actor"]
    if a["kind"] == "agent":
        who = "u" if a.get("unattended") else "a"
    elif a["kind"] == "person":
        who = a["id"][:8]
    else:
        who = "h"
    detail = ""
    t = e["type"]
    if t.startswith("task."):
        detail = e["task"]
    elif t.startswith("artifact."):
        detail = e["name"]
    elif t == "log.added":
        detail = short(e["text"], 40)
    elif t == "question.asked":
        detail = e["question"]["id"]
    elif t in ("question.answered", "gate.approved", "gate.changes_requested", "verdict.given", "gate.invalidated"):
        detail = decisions.line(e, 80).split(" ", 1)[1]
    return f"#{e['seq']} {t} {who}" + (f" {detail}" if detail else "")


def verified(c: Any, uids: Any, chunk: int = 40) -> Any:
    """Yield ``(uid, view)`` for candidates (from the index or a file scan, hints), loading and verifying them a chunk
    at a time; a ticket that is gone or that the actor may not see is skipped, so nothing unverified or hidden gets
    through to a caller that prints."""
    batch: list[str] = []
    it = iter(uids)
    while True:
        batch = [u for _, u in zip(range(chunk), it, strict=False)]
        if not batch:
            return
        c.store.load(batch)
        for uid in batch:
            v = c.store.ticket(uid)
            if v is not None and c.sees(v):
                yield uid, v


def next_ticket(c: Any, *, mine_first: bool = True) -> Any | None:
    """The ticket to work on: the session's own claim (``mine_first``), else the best free one the actor may see.

    Free means ``open`` with no live claim and nothing it is blocked by still undone; the best is the highest
    priority, then the lowest number. Candidates come from the index; the one returned (and every blocker looked at) is
    loaded and verified."""
    if mine_first:
        mine = c.mine()
        if mine:
            return mine[0]
    rows = c.store.index.query("SELECT uid, key, priority FROM tickets WHERE status = 'open'")
    rows.sort(key=lambda r: (PRIORITY.get(r[2], 2), key_number(r[1])))
    for _uid, v in verified(c, (r[0] for r in rows)):
        if v.status != "open" or (v.claim is not None and v.claim.live):
            continue
        blockers = [c.store.ticket(k) for k in v.fields["blocked_by"]]
        if all(b is None or b.status in ("done", "closed") for b in blockers):
            return v
    return None
