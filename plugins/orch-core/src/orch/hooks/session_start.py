from __future__ import annotations

from orch.core import query
from orch.core.rules import render_rules


def _claimed(entry) -> str:
    from orch.core import tasks as tk
    from orch.core.model import parse_ticket
    try:
        items = tk.ticket_tasks(parse_ticket(entry.path.read_text(encoding="utf-8")))
    except Exception:  # never break a session start over one ticket
        return f"{entry.id} ({entry.status})"
    doing, nxt = tk.doing(items), tk.next_task(items)
    where = f"doing {doing.id}" if doing else f"next {nxt.id}" if nxt else "no tasks yet" if not items else "all tasks closed"
    return f"{entry.id} ({entry.status}, {where})"


def _quick_lines(ws, session: str) -> list[str]:
    from orch.core import quick
    cfg = quick.settings(ws)
    if not cfg["enabled"]:
        return []
    tasks = [t for t in quick.all_tasks(ws) if t["status"] == "open"]
    mine = [t["id"] for t in tasks if (quick.active_claim(t, cfg["claim_minutes"]) or {}).get("session") == session]
    return [f"Quick tasks: {len(tasks)} open" + (f" · held by this session: {', '.join(mine)}" if mine else "")
            + " (`orch quick`; one at a time, never while your ticket is still in progress)"]


def session_start_text(ws, session: str) -> str:
    lines = ["# orch workspace", "", render_rules(ws.config), ""]
    mine = query.list_tickets(ws, session=session)
    claimed = ", ".join(_claimed(e) for e in mine) if mine else "none"
    lines.append(f"Tickets claimed by this session: {claimed}")
    items = query.waiting(ws)  # blocking items first, then later, then backlog grooming (#11)
    n = query.counts(items)
    lines.append(f"Waiting on the human: {n['blocking']} blocking · {n['backlog']} in the backlog"
                 + (f" · {n['later']} later" if n["later"] else ""))
    for i in items[:5]:
        detail = f" {i['detail']}" if i["detail"] else ""
        lines.append(f"- {i['ticket']}: {query.NEEDS_LABELS[i['kind']]}{detail}")
    try:
        lines += _quick_lines(ws, session)
    except Exception:  # never break a session start over quick tasks
        pass
    try:
        from orch.onboarding import open_setup_items
        open_items = open_setup_items(ws)
    except Exception:
        open_items = []  # onboarding is best-effort: never let it break session start
    if open_items:
        lines += ["", "Setup still open:"]
        lines += [f"- {c.code}: {c.message}" + (f" → {c.fix}" if c.fix else "") for c in open_items]
        lines.append("Mention these to the user once; they can hide one with `orch setup --dismiss <code>`.")
    lines += ["", "Use `orch next`, `orch show <id>` and the orch-tickets skill. The items above are the human's to do, not yours."]
    return "\n".join(lines)
