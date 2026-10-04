"""`orch check` findings for a ticket's task list (level, code, message); check.py wraps them."""
from __future__ import annotations

from orch.core import tasks as tk

_REF_KINDS_CHECKED = ("file", "static", "artifact", "ticket", "ac", "q")
MISSING_AFTER_MINUTES = 30


def task_findings(ws, t, status: str, entries) -> list[tuple[str, str, str]]:
    from orch.clock import now, parse_stamp
    from orch.core.gates import gate_state, plan_required
    from orch.core.ops import claim_expired
    from orch.core.tasks_view import resolve_ref

    try:
        items = tk.ticket_tasks(t)
    except tk.TaskParseError as e:
        return [("error", "tasks-parse", e.message)]
    out: list[tuple[str, str, str]] = []
    open_ = tk.open_ids(items)
    if status in ("testing", "done") and open_:
        out.append(("error", "tasks-open-at-testing", f"ticket is {status} with open tasks: {', '.join(open_)}"))
    doing = [x.id for x in items if x.state == "doing"]
    if len(doing) > 1:
        out.append(("warning", "tasks-multiple-doing", f"more than one task in progress: {', '.join(doing)}"))
    for x in items:
        if x.state in ("skipped", "blocked") and not x.why:
            out.append(("warning", "task-reason-missing", f"{x.id} is {x.state} without a why: line"))
    unknown, cycle = tk.needs_problems(items)
    out += [("warning", "task-needs-unknown", f"{a} needs {b}, which does not exist") for a, b in unknown]
    if cycle:
        out.append(("warning", "task-needs-cycle", "needs form a cycle: " + " → ".join(cycle)))
    for x in items:
        if x.closed:
            continue  # a done or skipped task's refs are history
        for r in x.refs:
            if r.kind in _REF_KINDS_CHECKED and not resolve_ref(ws, t, r, entries)["exists"]:
                out.append(("warning", "task-ref-missing", f"{x.id} ref {r.kind}:{r.target} does not exist"))
    claim = t.meta.get("claim") or {}
    expired = not claim.get("session") or claim_expired(claim, float(ws.config["claims"]["ttl_hours"]))
    if doing and status in ("in-progress", "waiting") and expired:
        out.append(("warning", "task-doing-stale", f"{doing[0]} was in progress when the claim expired"))
    if status == "in-progress" and not items and claim.get("session") and not expired \
            and (not plan_required(ws, t) or gate_state(t, "plan") == "approved"):
        try:
            minutes = (now() - parse_stamp(str(claim.get("at")))).total_seconds() / 60
        except ValueError:
            minutes = 0
        if minutes > MISSING_AFTER_MINUTES:
            out.append(("warning", "tasks-missing", f"claimed {int(minutes)} min ago and no task list yet (orch task add)"))
    return out
