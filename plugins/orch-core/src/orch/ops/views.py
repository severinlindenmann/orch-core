"""Text for what the read operations show. Everything derived from a ticket (titles, section text, criteria, tasks,
questions, notes, artifact names) goes through :func:`orch.cli.render.fence`; what stays outside a fence is a key,
an id, a status or another token from a closed list, or a number.
"""

from __future__ import annotations

from typing import Any

from orch.cli import render
from orch.model.claims import in_family
from orch.ops import decisions
from orch.ops.runtime import short


def short_gate_hash(h: str | None) -> str:
    """The first 12 hex digits (48 bits) of the verified gate hash: what ``orch show`` prints per open gate and
    what the signing review prints last, so a person can compare the two (F1 5.7)."""
    return (h or "").removeprefix("sha256:")[:12] or "-"


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


def fill_hint(view: Any, gate: str) -> str | None:
    """The command that supplies the first thing an approval of ``gate`` still lacks, or ``None`` when it lacks nothing
    (or lacks only an artifact, which has no one-line command)."""
    for m in view.gates[gate].missing:
        if m == "acceptance":
            return "orch ac add TEXT"
        if m == "tasks":
            return "orch task add TEXT"
        if not m.startswith("artifact:"):
            return f"orch section set {m} -m TEXT"
    return None


def claim_hint(c: Any) -> str:
    """What to do when there is no claim: claim a ticket that is free, else create one (never a command that fails)."""
    return "orch claim --next" if next_ticket(c, mine_first=False) is not None else 'orch new "TITLE"'


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
        if g is None:
            return "orch submit"
        return fill_hint(view, g) or f'orch ask "approve the {g} gate?"'  # an incomplete gate cannot be approved yet
    g = open_gate(view)
    if (
        g is not None
        and view.claim is not None
        and view.claim.live
        and session
        and in_family(session, view.claim.session)
    ):
        return fill_hint(view, g) or "orch show"
    return "orch show"


def task_line(t: Any, n: int = 48) -> str:
    return f"{t.id} {t.state}{' (' + ','.join(t.proves) + ')' if t.proves else ''}: {short(t.text, n)}"


def ac_line(a: Any, n: int = 48) -> str:
    return f"{a.id} [{'x' if a.evidence else ' '}] {short(a.text, n)}"


def question_line(q: Any, texts: dict[str, Any]) -> str:
    return f"{q.id} {'blocking' if q.blocking else 'open'} to={q.to}: {short(texts.get(q.id, {}).get('text', ''), 80)}"


def fenced_tasks(view: Any, label: str, n: int = 100) -> list[str]:
    return fence("\n".join(task_line(t, n) for t in view.tasks) or "no tasks", label)


def _items(raw: Any) -> dict[str, Any]:
    return {x["id"]: x for x in raw}


def _update_detail(e: dict[str, Any], state: dict[str, dict[str, Any]]) -> str:
    """What a ``ticket.updated`` changed, by field name and id (``title``, ``AC1 added``, ``T2 edited``,
    ``section plan``); ``state`` carries the criteria and tasks as they were before this event and is moved on."""
    parts: list[str] = []
    for path, val in e.get("set", {}).items():
        name = path.removeprefix("ticket.")
        if name in ("acceptance", "tasks") and isinstance(val, list):
            old, new = state.setdefault(name, {}), _items(val)
            parts += [f"{i} added" for i in new if i not in old]
            parts += [f"{i} edited" for i in new if i in old and new[i] != old[i]]
            parts += [f"{i} removed" for i in old if i not in new]
            state[name] = new
        else:
            parts.append(name)
    parts += [f"section {sid}" for sid in e.get("sections", {})]
    return ", ".join(parts)


def event_line(e: dict[str, Any], state: dict[str, dict[str, Any]] | None = None) -> str:
    """One event as ``#seq type actor: detail``. The actor is ``agent`` (``agent (unattended)`` without a grant),
    ``person`` and the first characters of the person id, or ``host``. The detail is a task, a question, an artifact
    name, the start of a note, what a decision decided or, for ``ticket.updated``, what changed (``AC1 added``)."""
    a = e["actor"]
    if a["kind"] == "agent":
        who = "agent (unattended)" if a.get("unattended") else "agent"
    elif a["kind"] == "person":
        who = f"person {a['id'][:8]}"
    else:
        who = "host"
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
    elif t == "ticket.updated":
        detail = _update_detail(e, {} if state is None else state)
    elif t in ("question.answered", "gate.approved", "gate.changes_requested", "verdict.given", "gate.invalidated"):
        detail = decisions.line(e, 80).split(" ", 1)[1]
    return f"#{e['seq']} {t} {who}" + (f": {detail}" if detail else "")


def event_lines(all_events: list[dict[str, Any]], after: int) -> list[str]:
    """The lines of the events after ``after``; the earlier ones are read only to know what an update changed."""
    state: dict[str, dict[str, Any]] = {}
    out = []
    for e in all_events:
        if e["seq"] > after:
            out.append(event_line(e, state))
        elif e["type"] == "ticket.updated":
            _update_detail(e, state)
    return out


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
