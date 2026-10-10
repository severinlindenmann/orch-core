"""Who may append which event type: the actor column of format doc 5.4, as data.

``P`` person only (signed), ``A`` an agent with a grant, or a person (signed), ``U`` also an unattended agent,
``H`` the host only. A type with several letters is allowed to each of them. ``tests/ops/test_actors.py`` parses
the F1 tables and fails when this drifts.
"""

from __future__ import annotations

__all__ = ["EVENT_ACTORS", "allows"]

EVENT_ACTORS: dict[str, frozenset[str]] = {
    t: frozenset(a)
    for t, a in {
        "ticket.created": "A",
        "ticket.updated": "A",
        "status.changed": "A",
        "ticket.submitted": "A",
        "ticket.closed": "P",
        "ticket.reopened": "P",
        "visibility.changed": "P",
        "people.changed": "P",
        "policy.changed": "P",
        "claim.taken": "A",
        "claim.released": "AH",
        "task.started": "A",
        "task.done": "A",
        "task.skipped": "A",
        "task.blocked": "A",
        "task.reopened": "A",
        "handoff.written": "A",
        "log.added": "AU",
        "question.asked": "AU",
        "question.answered": "P",
        "gate.approved": "P",
        "gate.changes_requested": "P",
        "verdict.given": "P",
        "gate.invalidated": "H",
        "branch.pushed": "H",
        "artifact.added": "AU",
        "artifact.replaced": "A",
        "edit.external": "H",
        "projection.repaired": "H",
        "restore": "P",
        "invalid.acknowledged": "P",
        "workspace.created": "P",
        "member.added": "P",
        "member.removed": "P",
        "role.changed": "P",
        "device.added": "P",
        "device.removed": "P",
        "device.revoked": "PH",
        "settings.changed": "P",
        "grant.issued": "P",
        "grant.revoked": "P",
        "addon.granted": "P",
        "addon.disabled": "P",
        "addon.purged": "P",
    }.items()
}


def allows(who: str, event_type: str) -> bool:
    """May an operation with this ``who`` append ``event_type``? A person (``human``) may append the types an agent
    may (marked A) and the person-only ones; ``agent`` only A; ``unattended`` only U; ``read`` nothing."""
    letters = EVENT_ACTORS[event_type]
    if who == "agent":
        return "A" in letters
    if who == "unattended":
        return "U" in letters
    if who == "human":
        return "P" in letters or "A" in letters
    return False
