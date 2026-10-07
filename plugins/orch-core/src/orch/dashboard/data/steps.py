"""The ticket page's gate step model: the five steps, whose move it is, the plan checklist and
the meta line under the title. Pure functions over a ticket and its needs_you() items."""
from __future__ import annotations

import re
from datetime import datetime

from orch.clock import parse_stamp
from orch.core.gates import GATE_SECTIONS, REAPPROVE_IN_PLACE, changes_pending, gate_state, human_questions_in, requirements_skipped
from orch.core.lifecycle import unanswered_blocking

STEP_NAMES = ("Requirements", "Plan", "Work", "Testing", "Done")
STATUS_ORDER = {s: i for i, s in enumerate(("backlog", "open", "in-progress", "waiting", "testing", "done"))}
YOUR_TURN = "Your turn"
CHANGES_REQUESTED = "Changes requested"


def when(value) -> datetime | None:
    """A stored stamp as an aware UTC datetime, or None when it is missing or does not parse."""
    try:
        return parse_stamp(value) if isinstance(value, str) else None
    except ValueError:
        return None


def day(value) -> str:
    """A stored stamp as the server's local "DD.MM", or "" when it does not parse."""
    at = when(value)
    return at.astimezone().strftime("%d.%m") if at else ""


def _gates(ticket) -> dict:
    g = ticket.meta.get("gates")
    return g if isinstance(g, dict) else {}


def _claim(ticket) -> dict:
    c = ticket.meta.get("claim")
    return c if isinstance(c, dict) else {}


def steps(ticket, *, plan_skip_sizes=(), requirements_skip_sizes=()) -> list[dict]:
    """The five-step tracker: Requirements, Plan, Work, Testing, Done (an epic: without Plan).

    A step is done when its gate is approved or the ticket's status is already past it
    (backlog < open < in-progress < waiting < testing < done). Plan is "skipped" when the size is
    in `plan_skip_sizes` and no plan was ever approved (an approved plan edited later shows as changed); Requirements
    likewise for `requirements_skip_sizes` (#172). Exactly one step is current: the first one that
    is neither done nor skipped, or Done once the ticket is done. A gate or verdict that waits for
    the human says "Your turn"; a gate the human sent back says "Changes requested".
    """
    status = ticket.status
    rank = STATUS_ORDER.get(status, 0)
    req, plan = gate_state(ticket, "requirements"), gate_state(ticket, "plan")
    skipped = ticket.meta.get("size") in tuple(plan_skip_sizes) and plan == "pending"
    req_skipped = req == "pending" and requirements_skipped(ticket.meta.get("size"), requirements_skip_sizes,
                                                            ticket.meta.get("type"))
    claim = _claim(ticket)

    def gate_note(gate: str, state: str, waits: bool) -> str:
        if state == "approved":
            on = day((_gates(ticket).get(gate) or {}).get("approved"))
            return f"Approved {on}" if on else "Approved"
        if changes_pending(ticket, gate):
            return CHANGES_REQUESTED
        if waits:
            return YOUR_TURN
        return "Changed since approval" if state == "invalidated" else ""

    req_waits = (status == "backlog" and not unanswered_blocking(ticket)
                 and bool(ticket.section("Requirements").strip()) and bool(ticket.section("Acceptance criteria").strip()))
    plan_waits = status in ("in-progress", "waiting") and bool(ticket.section("Plan").strip())
    rows = [
        ("skipped" if req_skipped else "done" if req == "approved" or rank >= STATUS_ORDER["open"] else None,
         "skipped" if req_skipped else gate_note("requirements", req, req_waits)),
        ("skipped" if skipped else "done" if plan == "approved" or rank >= STATUS_ORDER["testing"] else None,
         "skipped" if skipped else gate_note("plan", plan, plan_waits)),
        ("done" if rank >= STATUS_ORDER["testing"] else None,
         f"claimed by {claim.get('harness') or 'an agent'}" if claim.get("session") else ""),
        ("done" if status == "done" else None, YOUR_TURN if status == "testing" else ""),
        (None, ""),  # Done is never "done": it becomes the current step once the ticket is done
    ]
    out, seen_current = [], False
    epic = ticket.meta.get("type") == "epic"
    for name, (fixed, note) in zip(STEP_NAMES, rows):
        if epic and name == "Plan":
            continue  # an epic has no plan of its own; its children do
        state = fixed or ("todo" if seen_current else "current")
        seen_current = seen_current or state == "current"
        out.append({"name": name, "state": state, "note": note})
    return out


# ---------- whose move ----------

_APPROVE_TEXT = {
    "requirements": "The requirements are ready. Approve them so an agent can start, or request changes.",
    "plan": "The plan is ready. Approve it so an agent can start the work, or request changes.",
}


# F4: one line, per needs_you kind, saying why the ticket waits on the human (rule text, never agent prose). Board's
# Needs you lane, Today's cards and the ticket's move chip all use it.
_WHY = {
    "approve-requirements": "The agent drafted the requirements; nobody starts until you approve them.",
    "approve-plan": "The agent stopped before its tasks; it starts once you approve the plan.",
    "re-approve": "The {gate} changed after your approval; the work waits until you approve it again.",
    "approve-epic": "The epic changed after your approval; its children wait for you.",
    "answer": "The agent asked a blocking question and waits for your answer.",
    "verdict": "The work is in testing; only you accept it or send it back.",
    "task": "A task in the plan is yours; the agent waits for it.",
    "broken": "The ticket file cannot be read; nobody works on it until it is repaired.",
    "confirm": "The agent went ahead on its recommendation; confirm it or change it.",
    "stale-claim": "The agent holding it has gone silent; release the claim or check on it.",
}
_WHY_TOGETHER = "The agent drafted the requirements and the plan; one approval lets the work start."
_WHY_LATER_TASK = "A task in the plan is yours; the agent still has its own work first."


def why_waiting(item: dict | None, *, together: bool | None = None) -> str:
    """Why a needs_you() item waits on the human, in one line."""
    if not item:
        return ""
    kind = item.get("kind")
    if kind == "approve-requirements" and (item.get("together") if together is None else together):
        return _WHY_TOGETHER
    if kind == "task" and item.get("scope") == "later":
        return _WHY_LATER_TASK
    return _WHY.get(kind, "").format(gate=need_gate(item) or "gate")


def _own(ticket, needs_items) -> list[dict]:
    """This ticket's items that make it the human's move. A "confirm" item (#12: the agent went ahead on a non-blocking
    question) and a silent claim never do: the agent's move stays the agent's."""
    return [i for i in needs_items or [] if isinstance(i, dict) and str(i.get("ticket", "")).upper() == ticket.id.upper()
            and i.get("kind") not in ("confirm", "stale-claim", "factory-ready", "factory-stopped")]


def need_gate(item: dict) -> str | None:
    """The gate a needs_you() approval item is about ("requirements" | "plan"), else None."""
    kind = item.get("kind")
    if kind == "re-approve":
        gate = str(item.get("detail") or "")
    elif kind == "approve-epic":
        gate = "requirements"
    elif kind in ("approve-requirements", "approve-plan"):
        gate = kind.removeprefix("approve-")
    else:
        return None
    return gate if gate in GATE_SECTIONS else None


def can_approve(ticket, gate: str, *, plan_skip_sizes=(), allow_override: bool = False) -> bool:
    """True when an Approve for `gate` makes sense now and Ops.approve would accept it.

    Repeats Ops.approve's own checks, read-only (requirements: backlog, Requirements and Acceptance
    criteria filled, no blocking question open; plan: in progress or waiting, Plan filled). On top: the
    gate is not already approved, the human's change request is not still pending (that is the
    agent's move), and a first plan approval is only offered when the size needs a plan."""
    state = gate_state(ticket, gate)
    if state == "approved" or changes_pending(ticket, gate) or (human_questions_in(ticket, gate) and not allow_override):
        return False
    if gate == "requirements":
        # changed requirements are re-approved where the ticket stands (Ops.approve: invalidated only, #208)
        placed = ticket.status == "backlog" or (state == "invalidated" and ticket.status in REAPPROVE_IN_PLACE)
        return (placed and not unanswered_blocking(ticket)
                and bool(ticket.section("Requirements").strip()) and bool(ticket.section("Acceptance criteria").strip()))
    if gate == "plan":
        needed = ticket.meta.get("size") not in tuple(plan_skip_sizes) or state == "invalidated"
        return (ticket.status in ("in-progress", "waiting") and needed and not unanswered_blocking(ticket)
                and bool(ticket.section("Plan").strip()))
    return False


def reapprove_hint(ticket, gate: str, moves) -> dict:
    """An invalidated gate that cannot be approved in the current status: say how to get there."""
    status = ticket.status
    asks = human_questions_in(ticket, gate)
    if asks:
        return {"text": f"The {gate} has a line that reads as an open question for you (\u201c{asks[0][:80]}\u201d). "
                        "Request changes so the agent asks it with `orch ask`, or, if it is not a question for you, "
                        "approve with the box ticked.",
                "action": {"kind": "hint", "gate": gate}}
    if gate == "requirements" and status not in ("backlog",) + REAPPROVE_IN_PLACE:
        return {"text": "Requirements changed after approval, and a ticket in this status cannot be re-approved: "
                        "reopen it, or send it back, first.",
                "action": {"kind": "hint", "gate": gate}}
    elif gate == "plan" and status == "testing":
        return {"text": "Plan changed after approval — send the work back to in progress to re-approve.",
                "action": {"kind": "hint", "gate": gate}}
    elif gate == "plan" and status not in ("in-progress", "waiting"):
        text = "Plan changed after approval — move it back to in progress to re-approve."
        to, label = "in-progress", "Move back to in progress"
    else:
        what = ("Requirements and Acceptance criteria are filled and no blocking question is open"
                if gate == "requirements" else "the Plan is filled")
        return {"text": f"The {gate} changed since you approved it. You can approve again once {what}.",
                "action": {"kind": "hint", "gate": gate}}
    if to in tuple(moves or ()):
        return {"text": text, "action": {"kind": "move", "gate": gate, "to": to, "label": label}}
    return {"text": text, "action": {"kind": "hint", "gate": gate}}


def _candidate(ticket, item: dict, plan_skip_sizes, moves) -> dict:
    kind, detail = item.get("kind"), str(item.get("detail") or "")
    gate = need_gate(item)
    if ticket.meta.get("type") == "epic" and kind in ("approve-requirements", "approve-epic"):
        # an epic's approval is its charter: the epic and its children, read in full on the epic page
        text = ("Read the epic and every open child, then approve the epic (or request changes)."
                if kind == "approve-requirements" else
                "Something changed since you approved the epic" + (f" ({detail})" if detail else "")
                + ". Read what changed, then approve the epic again.")
        label = "Approve the epic" if kind == "approve-requirements" else "Re-approve the epic"
        return {"text": text, "action": {"kind": "approve", "gate": "requirements", "label": label}, "rank": 0}
    if gate:
        if not can_approve(ticket, gate, plan_skip_sizes=plan_skip_sizes):
            return {**reapprove_hint(ticket, gate, moves), "rank": 1}
        if kind == "re-approve":
            text = f"The {gate} changed since you approved it. Approve it again, or request changes."
            label = f"Re-approve {gate}"
        elif kind == "approve-requirements" and item.get("together"):
            return {"text": "The agent drafted the requirements and the plan. Read both, then approve them together "
                            "(or request changes).",
                    "action": {"kind": "approve", "gate": gate, "together": True,
                               "label": "Approve requirements and plan"}, "rank": 0}
        else:
            text, label = _APPROVE_TEXT[gate], f"Approve {gate}"
        return {"text": text, "action": {"kind": "approve", "gate": gate, "label": label}, "rank": 0}
    if kind == "answer":
        qids = [q.strip() for q in detail.split(",") if q.strip()]
        qid = qids[0] if qids else ""
        n = len(qids)
        text = (f"{'A question blocks' if n == 1 else f'{n} questions block'} the work: "
                f"{', '.join(qids)}. Answer so the agent can go on.")
        return {"text": text, "rank": 0,
                "action": {"kind": "answer", "qid": qid, "label": f"Answer {qid}".strip(), "href": f"#q-{qid}"}}
    if kind == "verdict":
        return {"text": "The work is ready for testing. Check it, then accept it or send it back.", "rank": 0,
                "action": {"kind": "verdict", "label": "Accept"}}
    if kind == "broken" and detail.startswith("Tasks line "):
        return {"text": f"The Tasks section cannot be read. Repair it in the raw file. ({detail})",
                "rank": 0, "action": {"kind": "repair", "label": "Repair the Tasks section", "href": f"/t/{ticket.id}/raw"}}
    if kind == "broken":
        return {"text": "The ticket file cannot be read. Repair it in the raw file." + (f" ({detail})" if detail else ""),
                "rank": 0, "action": {"kind": "repair", "label": "Repair the file", "href": f"/t/{ticket.id}/raw"}}
    if kind == "task":
        from orch.core import tasks as tk
        from orch.errors import OrchError
        try:
            what = tk.find(tk.ticket_tasks(ticket), detail).text
        except OrchError:
            what = ""
        return {"text": f"{detail} is yours: {what}. Do it, then mark it done." if what
                else f"{detail} is yours. Do it, then mark it done.", "rank": 0,
                "action": {"kind": "task", "task": detail, "label": f"Go to {detail}", "href": f"#task-{detail}"}}
    return {"text": "Something on this ticket waits for you.", "action": None, "rank": 0}


def _agent_text(ticket, plan_skip_sizes, requirements_skip_sizes=()) -> str:
    for gate in GATE_SECTIONS:
        if changes_pending(ticket, gate):
            cr = (_gates(ticket).get(gate) or {}).get("changes_requested") or {}
            message = str(cr.get("message") or "").strip()
            return (f"You asked for changes on the {gate}" + (f": {message}" if message else "")
                    + f". The agent updates the {gate} next; you look again after that.")
    status = ticket.status
    plan_needed = ticket.meta.get("size") not in tuple(plan_skip_sizes)
    req_skipped = (gate_state(ticket, "requirements") == "pending"
                   and requirements_skipped(ticket.meta.get("size"), requirements_skip_sizes, ticket.meta.get("type")))
    if status == "backlog":
        if req_skipped:
            return "This size skips the requirements gate. Move it to open when an agent should pick it up."
        return "An agent refines the requirements next; you approve them after that."
    if status == "open":
        done = "This size skips the requirements gate." if req_skipped else "Requirements are approved."
        return (f"{done} An agent writes the plan next; you approve it after that." if plan_needed
                else f"{done} An agent picks up the work next.")
    if status in ("in-progress", "waiting"):
        if plan_needed and gate_state(ticket, "plan") != "approved" and not ticket.section("Plan").strip():
            return "An agent writes the plan next; you approve it after that."
        if status == "waiting":
            return "The ticket waits on a blocker. The agent goes on once it is resolved."
        return "An agent does the work next; you give the verdict in Testing."
    if status == "done":
        return "This ticket is done."
    return "An agent takes the next step."


def your_move(ticket, needs_items, *, plan_skip_sizes=(), moves=(), requirements_skip_sizes=()) -> dict:
    """Whose move it is: {"kind": "human" | "agent", "text", "action": dict | None, "more": [...]}.

    `needs_items` are query.needs_you() items (other tickets' items are ignored); in needs_you's
    urgency order, the first one the human can act on directly decides the move, and items that
    only have a hint (a changed gate that cannot be approved in this status) come after it.
    `moves` are the statuses the human may move the ticket to (for "Move back to ..." buttons).
    `action`: {"kind": "approve", "gate", "label"}, {"kind": "answer", "qid", "label", "href"},
    {"kind": "verdict", "label"}, {"kind": "repair", "label", "href"}, {"kind": "task", "task", "label", "href"},
    {"kind": "move", "gate", "to", "label"} or {"kind": "hint", "gate"}; None on the agent's move.
    `more`: the other hint items as [{"text", "action"}], shown as secondary lines.
    """
    own = _own(ticket, needs_items)
    if own:
        cands = sorted((_candidate(ticket, i, plan_skip_sizes, moves) for i in own), key=lambda c: c["rank"])
        first = cands[0]
        more = [{"text": c["text"], "action": c["action"]} for c in cands[1:] if c["rank"] == 1]
        return {"kind": "human", "text": first["text"], "action": first["action"], "more": more}
    return {"kind": "agent", "text": _agent_text(ticket, plan_skip_sizes, requirements_skip_sizes), "action": None,
            "more": []}


# ---------- plan checklist ----------

_ITEM = re.compile(r"^[-*+]\s+\[([ xX])\]\s+(.*)$")


_SUB = re.compile(r"^[-*+]\s+(.*)$")


def plan_checklist(text: str | None) -> list[dict] | None:
    """`- [x]` / `- [ ]` items as [{"text", "state": "done" | "now" | "todo", "subs": [str]}]; the
    first unchecked item is "now". An indented plain bullet becomes a sub-line of the item above;
    other indented lines continue the item (or its last sub-line). None when the Plan has no
    checkbox items, or has free text or nested checkboxes around them (it then renders as plain
    Markdown, so nothing is lost)."""
    items: list[dict] = []
    for line in (text or "").split("\n"):
        if not line.strip():
            continue
        indented = line[:1].isspace()
        m = _ITEM.match(line.strip())
        if m and not indented:
            items.append({"text": m.group(2).strip(), "checked": m.group(1) != " ", "subs": []})
        elif items and indented and not m:
            sub = _SUB.match(line.strip())
            if sub:
                items[-1]["subs"].append(sub.group(1).strip())
            elif items[-1]["subs"]:
                items[-1]["subs"][-1] += " " + line.strip()
            else:
                items[-1]["text"] += " " + line.strip()
        else:  # free text, or a nested checkbox: render the plan as Markdown instead
            return None
    if not items:
        return None
    out, now_given = [], False
    for i in items:
        state = "done" if i["checked"] else ("todo" if now_given else "now")
        now_given = now_given or state == "now"
        out.append({"text": i["text"], "state": state, "subs": i["subs"]})
    return out


# ---------- meta line ----------

def _span(minutes: float) -> str:
    minutes = max(0, int(minutes))
    if minutes < 60:
        return f"{minutes} min"
    if minutes < 24 * 60:
        return f"{minutes // 60} h"
    return f"{minutes // (24 * 60)} d"


def _since(value, now: datetime) -> str | None:
    at = value if isinstance(value, datetime) else when(value)
    return _span((now - at).total_seconds() / 60) if at else None


def _need_started(item: dict, events) -> datetime | None:
    """When the need began: the newest event that created it (the gate's section edited or the
    move into the gate's status, a question asked, the move to testing), or None."""
    kind, gate = item.get("kind"), need_gate(item)
    data = lambda e: e.data if isinstance(getattr(e, "data", None), dict) else {}
    if gate:
        status = "backlog" if gate == "requirements" else "in-progress"
        match = lambda e: ((e.kind == "ticket.edited" and data(e).get("section") in GATE_SECTIONS[gate])
                           or data(e).get("to") == status)
    elif kind == "answer":
        match = lambda e: e.kind == "question.asked"
    elif kind == "verdict":
        match = lambda e: data(e).get("to") == "testing"
    else:
        return None
    stamps = [at for e in events or [] if match(e) and (at := when(getattr(e, "at", None)))]
    return max(stamps) if stamps else None


def meta_line(ticket, needs_items, now: datetime, *, events=None) -> str:
    """The muted line under the title, built only from facts that apply:
    "open 2 d · waiting on you 5 h · 1 open question". `events`: this ticket's events; "waiting on
    you" counts from the event that created the first need, else from the ticket's `updated`."""
    parts = []
    if ticket.status != "done":
        age = _since(ticket.meta.get("created"), now)
        if age:
            parts.append(f"open {age}")
    own = _own(ticket, needs_items)
    if own:
        waited = _since(_need_started(own[0], events) or ticket.meta.get("updated"), now)
        parts.append(f"waiting on you {waited}" if waited else "waiting on you")
    open_qs = [q for q in ticket.meta.get("questions") or []
               if isinstance(q, dict) and q.get("answer") in (None, "", [])]
    if open_qs:
        parts.append(f"{len(open_qs)} open question" + ("" if len(open_qs) == 1 else "s"))
    return " · ".join(parts)
