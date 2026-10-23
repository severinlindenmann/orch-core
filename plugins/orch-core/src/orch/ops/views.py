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
from orch.store.store import LoadBudgetExceeded


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


CLAIM_HINT_SCAN = 40  # visible open candidates
CLAIM_HINT_WORK = 120  # ticket reads and key ownership checks, including dependencies


def claim_hint(c: Any) -> str:
    """What to do when there is no claim: claim a ticket that is free, else create one (never a command that fails).
    It looks at the best :data:`CLAIM_HINT_SCAN` visible open tickets only; when none is free and there are more,
    it says ``orch next``, which looks at all of them."""
    try:
        with c.store.load_budget(CLAIM_HINT_WORK):
            view, complete = _free_ticket(c, CLAIM_HINT_SCAN)
    except LoadBudgetExceeded:
        return "orch next"
    if view is not None:
        return "orch claim --next"
    return 'orch new "TITLE"' if complete else "orch next"


def human_next_hint(c: Any) -> str:
    """The ``next:`` line of a person's ``orch status`` with no claim: a person does not claim, so no agent verb."""
    return "start an agent from the dashboard (orch dashboard); an agent claims a ticket itself"


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
        # an incomplete gate cannot be approved yet; a complete one is approved by the person in Approvals: no question
        return fill_hint(view, g) or "orch wait"
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
            new = _items(val)
            if name not in state:
                parts.append(f"{name} set ({', '.join(new) or 'empty'})")
                state[name] = new
                continue
            old = state[name]
            parts += [f"{i} added" for i in new if i not in old]
            parts += [f"{i} edited" for i in new if i in old and new[i] != old[i]]
            parts += [f"{i} removed" for i in old if i not in new]
            state[name] = new
        else:
            parts.append(name)
    parts += [f"section {sid}" for sid in e.get("sections", {})]
    return ", ".join(parts)


#: how a hub channel's factor reads to a person (15.4 views): never the raw label
FACTOR_WORDS = {"se-unlocked": "phone, unlocked", "password+totp": "browser, password + code", "passkey": "passkey"}


def via_hub(e: dict[str, Any]) -> str:
    """``via hub · phone, unlocked`` for a hub-signed event, else ``""``."""
    factor = FACTOR_WORDS.get(str(e.get("channel_factor")), "")
    return "via hub" + (f" · {factor}" if factor else "") if e.get("auth") == "hub" else ""


def event_line(
    e: dict[str, Any], state: dict[str, dict[str, Any]] | None = None, names: dict[str, str] | None = None
) -> str:
    """One event as ``#seq type actor: detail``. The actor is ``agent`` (``agent (unattended)`` without a grant),
    ``person`` and the first characters of the person id, or ``host``. The detail is a task, a question, an artifact
    name, the start of a note, what a decision decided or, for ``ticket.updated``, what changed (``AC1 added``)."""
    a = e["actor"]
    if a["kind"] == "agent":
        who = "agent (unattended)" if a.get("unattended") else "agent"
    elif a["kind"] == "person" and "via" in a:  # a hub user: the member it is aliased to (names), else the id's start
        who = f"{(names or {}).get(a['id']) or 'hub user ' + a['id'][2:10]} {via_hub(e)}".rstrip()
    elif a["kind"] == "person":
        who = f"person {a['id'][:8]}"
    else:
        who = "host"
    detail = ""
    t = e["type"]
    if t.startswith("task."):
        detail = e["task"]
    elif t.startswith("artifact."):
        from orch.model.accept import parse_label, parse_withdraw_label

        detail = e["name"]
        if e.get("kind") == "feedback" and a["kind"] == "person":
            parsed = parse_label(e.get("label"))
            if parsed is not None and e.get("ac") == parsed[0]:
                detail = f"accepted {parsed[0]} manually"
            elif t == "artifact.replaced" and "ac" not in e:
                withdrawn = parse_withdraw_label(e.get("label"))
                if withdrawn:
                    detail = f"withdrew the manual acceptance of {withdrawn}"
    elif t == "hub.pinned":
        o = e["object"]["o"]
        detail = (
            "hub key/roles updated ("
            + o["kind"].removeprefix("hub.")
            + (f" v{o['version']}" if "version" in o else "")
            + ")"
        )
    elif t == "log.added":
        detail = short(e["text"], 40)
    elif t == "question.asked":
        detail = e["question"]["id"]
    elif t == "ticket.updated":
        detail = _update_detail(e, {} if state is None else state)
    elif t in ("question.answered", "gate.approved", "gate.changes_requested", "verdict.given", "gate.invalidated"):
        detail = decisions.line(e, 80).split(" ", 1)[1]
    return f"#{e['seq']} {t} {who}" + (f": {detail}" if detail else "")


def hub_names(ws: Any) -> dict[str, str]:
    """``u_<user>`` -> the name of the member it is aliased to (any hub's book, the C4 union), for the log lines."""
    out: dict[str, str] = {}
    hub = getattr(ws, "hub", None) or {}
    for book in (hub.get("alias_book") or {}).values():
        for person, user in book.items():
            m = ws.members.get("p_" + person)
            if m is not None:
                out["u_" + user] = m.name
    return out


def event_lines(
    all_events: list[dict[str, Any]],
    after: int,
    state: dict[str, dict[str, Any]] | None = None,
    names: dict[str, str] | None = None,
) -> list[str]:
    """The lines of the events after ``after``; the earlier ones are read only to know what an update changed."""
    state = {"acceptance": {}, "tasks": {}} if state is None else state
    out = []
    for e in all_events:
        if e["seq"] > after:
            out.append(event_line(e, state, names))
        elif e["type"] == "ticket.updated":
            _update_detail(e, state)
    return out


def log_lines(store: Any, uid: str, after: int) -> tuple[list[str], list[dict[str, Any]]]:
    """Read and render only the requested tail, once."""
    tail = store.events(uid, after=after)
    names = {name for name in ("acceptance", "tasks") if any(f"ticket.{name}" in e.get("set", {}) for e in tail)}
    state = store.event_fields_before(uid, after, names) if after and names else None
    return event_lines(tail, after, state, hub_names(store.state.workspace)), tail


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
    return _free_ticket(c)[0]


def _free_ticket(c: Any, limit: int | None = None) -> tuple[Any | None, bool]:
    """The best free ticket among the first ``limit`` visible open candidates (all without a limit), and whether every
    candidate was looked at."""
    # Hidden rows must not consume the scan cap or the verification budget. The index is only a hint;
    # verified() still checks the selected logs and their visibility before using them.
    rows = c.store.index.query(
        "SELECT uid, key, priority FROM tickets WHERE status = 'open' AND "
        "(json_extract(visibility, '$') = 'workspace' OR EXISTS "
        "(SELECT 1 FROM json_each(visibility, '$.restricted') WHERE value = ?))",
        (c.viewer,),
    )
    rows.sort(key=lambda r: (PRIORITY.get(r[2], 2), key_number(r[1])))
    complete = limit is None or len(rows) <= limit
    uids = [r[0] for r in (rows if limit is None else rows[:limit])]
    for _uid, v in verified(c, uids, chunk=len(uids) if limit is not None else 40):
        if v.status != "open" or (v.claim is not None and v.claim.live):
            continue
        blockers = [c.store.ticket(k) for k in v.fields["blocked_by"]]
        if all(b is None or b.status in ("done", "closed") for b in blockers):
            return v, True
    return None, complete
