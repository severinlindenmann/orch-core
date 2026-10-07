"""`orch status <id>`: the compact view an agent reads instead of parsing `orch show --json` (#218). A projection of
`schema.ticket_document` (so the numbers are the document's own), plus the claim's expiry (query.claim_expiry, #217)
and the cursor of the latest human decision (the one `orch wait` printed, #212). Read-only."""
from __future__ import annotations

from datetime import timedelta

from orch.clock import stamp_s


def _gate(g: dict) -> dict:
    cr = g.get("changes_requested")
    return {"state": g["state"], "approved": g.get("approved"),
            "changes_requested": cr["message"] if isinstance(cr, dict) else None}


def status_document(ws, ticket) -> dict:
    from orch.core import query, schema
    from orch.core.events import read_events
    from orch.core.wait import HUMAN_EVENT_KINDS
    doc = schema.ticket_document(ws, ticket)
    events = read_events(ws, ticket.id)
    gates = {name: _gate(doc["gates"][name]) for name in ("requirements", "plan")}
    gates["verify"] = {"state": doc["gates"]["verify"].get("verdict") or "pending", "approved": doc["gates"]["verify"].get("at"),
                       "changes_requested": None}
    claim = doc["claim"]
    held = bool(claim.get("session"))
    expired, last = query.claim_expiry(ws, ticket.id, ticket.status, ticket.meta.get("claim") or {}, events)
    ttl = float(ws.config["claims"]["ttl_hours"])
    forever = ticket.status == "waiting"
    claim_out = {"held": held, "harness": claim.get("harness"), "session": claim.get("session"), "at": claim.get("at"),
                 "last_activity": stamp_s(last) if held and last else None, "ttl_hours": ttl,
                 "expired": bool(held and expired),
                 "expires": None if not held or forever or last is None else stamp_s(last + timedelta(hours=ttl)),
                 "never_while_waiting": forever}
    tasks = doc["tasks"]
    human = [e.seq for e in events if e.kind in HUMAN_EVENT_KINDS and str(e.actor).startswith("human:")]
    return {
        "ticket": ticket.id, "title": ticket.title, "status": ticket.status,
        "move": doc["move"],
        "gates": gates,
        "questions": [{"id": q.get("id"), "text": q.get("text"), "blocking": bool(q.get("blocking")),
                       "answer": q.get("answer"), "note": q.get("note"), "answered": q.get("answered")}
                      for q in doc["questions"]],
        "claim": claim_out,
        "tasks": {"summary": tasks["summary"], "doing": tasks["doing"], "next": tasks["next"],
                  "can_move_to_testing": tasks["can_move_to_testing"]},
        "cursor": max(human) if human else None,
    }


def render_text(s: dict) -> str:
    """A few lines: the ticket, whose move, the gates, the open and answered questions, the claim, the tasks."""
    mv = s["move"]
    lines = [f"{s['ticket']} {s['title']} [{s['status']}]",
             f"move: {mv['who']} - {mv['kind']}" + (f" ({mv['why']})" if mv.get("why") else "")]
    lines.append("gates: " + ", ".join(
        f"{n} {g['state']}" + (f" ({g['changes_requested']})" if g["changes_requested"] else "")
        for n, g in s["gates"].items()))
    for q in s["questions"]:
        said = f"answered: {q['answer']}" + (f" ({q['note']})" if q["note"] else "") if q["answered"] else "OPEN"
        lines.append(f"{q['id']} {said} - {q['text']}")
    c = s["claim"]
    if not c["held"]:
        lines.append("claim: none")
    elif c["expired"]:
        lines.append(f"claim: {c['harness']} since {c['at']}, EXPIRED (no activity for {c['ttl_hours']:g} h)")
    else:
        end = "never while waiting" if c["never_while_waiting"] else f"expires {c['expires']}"
        lines.append(f"claim: {c['harness']} since {c['at']}, {end}")
    t = s["tasks"]["summary"]
    lines.append(f"tasks: {t['done']}/{t['total']} done" + (f", doing {s['tasks']['doing']}" if s["tasks"]["doing"] else "")
                 + (f", next {s['tasks']['next']}" if s["tasks"]["next"] else ""))
    lines.append(f"cursor: {s['cursor']}" if s["cursor"] is not None else "cursor: none")
    return "\n".join(lines)
