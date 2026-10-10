"""Needs (ticket-format §5.9, §10): what waits for whom. "Waiting" is derived and shown, not a status."""

from __future__ import annotations

from dataclasses import dataclass

from . import gates, generations, policies, questions
from .types import GATES, TCore, WsCore


@dataclass(frozen=True)
class Need:
    kind: str  # question | approve | no_eligible | acknowledge
    ticket: str | None  # uid; None for the workspace
    who: tuple[str, ...]  # persons who can act
    ref: str | None = None  # question id or gate
    detail: str = ""


def gate_waiting(ws: WsCore, t: TCore, g: str) -> bool:
    """The gate waits for a person's decision now."""
    if t.status in ("done", "closed") or not policies.applies(ws, t, g) or policies.blocked(ws, t, g):
        return False
    if generations.reached(ws, t, g):
        return False
    if g in ("requirements", "plan"):
        if g == "plan" and policies.applies(ws, t, "requirements") and not generations.reached(ws, t, "requirements"):
            return False
        return not gates.missing_for_gate(ws, t, g)
    if t.status != "testing":
        return False
    return g == "verify" or not policies.applies(ws, t, "verify") or generations.reached(ws, t, "verify")


def needs_of(ws: WsCore, t: TCore, frozen: bool = False) -> list[Need]:
    out: list[Need] = []
    for q in questions.open_questions(t):
        who = tuple(questions.addressees(ws, t, q.question))
        out.append(Need("question", t.uid, who, q.question["id"], "blocking" if q.question["blocking"] else ""))
    for g in GATES:
        if t.status in ("done", "closed") or not policies.applies(ws, t, g):
            continue
        if policies.blocked(ws, t, g):
            owners = tuple(sorted(p for p, m in ws.members.items() if m.role == "owner"))
            out.append(Need("no_eligible", t.uid, owners, g))
        elif gate_waiting(ws, t, g):
            out.append(Need("approve", t.uid, tuple(policies.eligible_persons(ws, t, g)), g))
    if frozen:
        owners = tuple(sorted(p for p, m in ws.members.items() if m.role == "owner"))
        out.append(Need("acknowledge", t.uid, owners, None, "invalid event in the ticket log"))
    return out


def waiting(ws: WsCore, t: TCore) -> bool:
    ns = needs_of(ws, t)
    return any((n.kind == "question" and n.detail == "blocking") or n.kind == "approve" for n in ns)


def workspace_needs(ws: WsCore, frozen: bool) -> list[Need]:
    if frozen:
        owners = tuple(sorted(p for p, m in ws.members.items() if m.role == "owner"))
        return [Need("acknowledge", None, owners, None, "invalid event in the workspace log")]
    return []
