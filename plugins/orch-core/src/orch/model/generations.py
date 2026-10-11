"""Gate generations (ticket-format §5.7): the syntactic raise table, at most +1 per event, in every status.

A handler records the gates its event raises *directly* with ``mark`` (``t.marks``); ``settle`` then applies the
rest of the table in one place: the cascade through earlier gates (an earlier raise or a change of its first
``count`` counting approvals raises every later gate), the "applies" filter, and the single ``+1``. It also fills
``pending_void`` (the counting approvals the event retired), which a later ``gate.invalidated`` must list exactly.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from . import policies
from .types import GATES, Decision, TCore, WsCore

_REQ_PATHS = {
    "ticket.type",
    "ticket.size",
    "ticket.acceptance",
    "body.summary",
    "body.context",
    "body.requirements",
    "body.out_of_scope",
}
BOUND_BASE: dict[str, set[str]] = {
    "requirements": _REQ_PATHS,
    "plan": _REQ_PATHS | {"ticket.tasks", "body.plan", "body.decisions"},
    "verify": {"ticket.type", "ticket.size", "ticket.acceptance", "ticket.links", "body.verification", "body.findings"},
    "code": {"ticket.type", "ticket.size", "ticket.acceptance", "ticket.links"},
}

_REQ = ("summary", "context", "requirements", "out_of_scope")
# Core sections of each gate by ticket type (§4 table); the gate hash input has exactly these keys.
GATE_SECTIONS: dict[str, dict[str, tuple[str, ...]]] = {
    "requirements": {
        "feature": _REQ,
        "bug": _REQ,
        "chore": ("summary", "context", "requirements"),
        "spike": ("summary", "context", "requirements"),
        "epic": _REQ,
    },
    "plan": {t: ("plan", "decisions") for t in ("feature", "bug", "chore", "spike")} | {"epic": ("decisions",)},
    "verify": {"feature": ("verification",), "bug": ("verification",), "chore": (), "spike": ("findings",), "epic": ()},
    "code": {t: () for t in ("feature", "bug", "chore", "spike", "epic")},
}


def active_addons(ws: WsCore):
    return [a for a in ws.addons.values() if a.enabled and not a.purged]


def addon_field_gates(ws: WsCore, gate: str) -> list[tuple[str, str]]:
    """``(addon, field)`` pairs whose ``binds`` names the gate."""
    return sorted((a.name, f) for a in active_addons(ws) for f, gs in a.binds["fields"].items() if gate in gs)


def addon_sections(ws: WsCore, gate: str, ticket_type: str) -> list[str]:
    return sorted(
        s["id"]
        for a in active_addons(ws)
        for s in a.binds["sections"]
        if gate in s["gate"] and ticket_type in s["types"]
    )


def addon_packages(ws: WsCore, gate: str, ticket_type: str) -> dict[str, str]:
    names = {a for a, _ in addon_field_gates(ws, gate)}
    names |= {s.split(".", 1)[0] for s in addon_sections(ws, gate, ticket_type)}
    return {n: ws.addons[n].package_sha256 for n in sorted(names)}


def sections_of(ws: WsCore, gate: str, ticket_type: str) -> list[str]:
    return [*GATE_SECTIONS[gate][ticket_type], *addon_sections(ws, gate, ticket_type)]


def bound(ws: WsCore, gate: str, ticket_type: str) -> set[str]:
    paths = set(BOUND_BASE[gate])
    paths |= {f"ticket.addons.{a}.{f}" for a, f in addon_field_gates(ws, gate)}
    paths |= {"body." + s for s in addon_sections(ws, gate, ticket_type)}
    return paths


def bound_any(ws: WsCore, ticket_type: str) -> set[str]:
    return set().union(*(bound(ws, g, ticket_type) for g in GATES))


def mark(t: TCore, *gates: str) -> None:
    t.marks.update(gates)


def mark_paths(ws: WsCore, t: TCore, paths: set[str]) -> set[str]:
    """Mark every gate one of whose bound paths is touched; returns those gates."""
    hit = {g for g in GATES if paths & bound(ws, g, t.ticket_type)}
    t.marks |= hit
    return hit


def mark_from(t: TCore, gate: str) -> None:
    """``g`` and every later gate (change requests, a failing verdict)."""
    t.marks.update(GATES[GATES.index(gate) :])


# --- counting ---------------------------------------------------------------------------------------------------


def counting(t: TCore, gate: str) -> list[Decision]:
    """Counting approvals: ``approve``/``pass`` at the current generation, not voided, one per person, by ``seq``."""
    gc = t.gates[gate]
    out, seen = [], set()
    for d in gc.decisions:
        if d.kind in ("approve", "pass") and d.gen == gc.gen and not d.voided and d.person not in seen:
            seen.add(d.person)
            out.append(d)
    return out


def first_count(ws: WsCore, t: TCore, gate: str) -> tuple[str, ...]:
    need = policies.effective(ws, t, gate)["count"]
    return tuple(sorted(d.id for d in counting(t, gate)[:need]))


def reached(ws: WsCore, t: TCore, gate: str) -> bool:
    return len(counting(t, gate)) >= policies.effective(ws, t, gate)["count"]


# --- settle -----------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Snap:
    applies: bool
    counting: frozenset[str]
    first: tuple[str, ...]


def snapshot(ws: WsCore, t: TCore) -> dict[str, Snap]:
    return {
        g: Snap(
            policies.applies(ws, t, g),
            frozenset(d.id for d in counting(t, g)),
            first_count(ws, t, g),
        )
        for g in GATES
    }


@dataclass(frozen=True)
class Settled:
    raised: tuple[str, ...]  # gates whose generation went up
    voided: tuple[str, ...]  # gates that lost a counting approval (``edit.external`` ``voided_gates``)


def settle(ws: WsCore, t: TCore, before: dict[str, Snap]) -> Settled:
    """Apply the raise table once for the event that just mutated ``t``."""

    def app(g: str) -> bool:
        return before[g].applies or policies.applies(ws, t, g)

    direct = {g for g in t.marks if app(g)}
    t.marks.clear()
    changed = {g for g in GATES if first_count(ws, t, g) != before[g].first}
    triggers = [GATES.index(g) for g in direct | changed]
    raised = set(direct)
    if triggers:
        first = min(triggers)
        raised |= {g for i, g in enumerate(GATES) if i > first and app(g)}
    for g in GATES:
        if g in raised:
            t.gates[g].gen += 1
    lost = []
    for g in GATES:
        gone = set(before[g].counting) - {d.id for d in counting(t, g)}
        t.gates[g].pending_void |= gone
        if gone:
            lost.append(g)
    return Settled(tuple(g for g in GATES if g in raised), tuple(lost))


# --- effects of workspace events on a ticket --------------------------------------------------------------------


SETTLED = ("done", "closed")


def _void_person_now(ws: WsCore, t: TCore, person: str) -> None:
    for g in GATES:
        mine = [d for d in counting(t, g) if d.person == person]
        if mine and not reached(ws, t, g):
            for d in mine:
                d.voided = True
            t.marks.add(g)


def void_person(ws: WsCore, t: TCore, person: str) -> None:
    """``member.removed`` / ``role.changed`` (§5.7): the person's counting approvals are voided on the gates that have
    not yet reached ``count``, on every unsettled ticket. A settled (done or closed) ticket keeps its state; the void
    waits in ``exempt_persons`` for the moment the ticket becomes unsettled other than by a reopen."""
    if t.status in SETTLED:
        t.exempt_persons.add(person)
        return
    _void_person_now(ws, t, person)


def _void_device_now(t: TCore, device: str) -> None:
    for g in GATES:
        mine = [d for d in counting(t, g) if d.device == device]
        for d in mine:
            d.voided = True
        if mine:
            t.marks.add(g)


def void_device(ws: WsCore, t: TCore, device: str) -> None:
    """``device.revoked`` (compromised): voids what the device signed on every unsettled ticket, reached or not. A
    settled ticket keeps its state; each gate lists the device's decisions that were counting (``revoked_flag``) and
    the void waits in ``exempt_devices`` (§5.7)."""
    if t.status in SETTLED:
        t.exempt_devices.add(device)
        for g in GATES:
            t.gates[g].revoked_flag |= {d.id for d in counting(t, g) if d.device == device}
        return
    _void_device_now(t, device)


def on_unsettle(ws: WsCore, t: TCore, before_status: str) -> None:
    """A settled ticket that becomes unsettled other than by ``ticket.reopened`` (a push after ``done``): the voids it
    was exempt from apply now, and ``settle`` raises each gate that loses a counting approval. A reopen raises every
    gate and clears the record itself."""
    if before_status in SETTLED and t.status not in SETTLED:
        for device in sorted(t.exempt_devices):
            _void_device_now(t, device)
        for person in sorted(t.exempt_persons):
            _void_person_now(ws, t, person)
        t.exempt_devices.clear()
        t.exempt_persons.clear()


def addon_binds_gates(binds: dict[str, Any] | None) -> set[str]:
    if not binds:
        return set()
    out = {g for gs in binds["fields"].values() for g in gs}
    out |= {g for s in binds["sections"] for g in s["gate"]}
    return out
