"""Whose ``verify`` command ``task done --run`` may execute (F1 "Receipts", host §0.8 "Trust of the command").

Any agent holding the claim can write a task's ``verify`` (``task.add --verify``, a batch item); no person signs it.
So the command runs only if

- **approved**: the plan gate applies and is approved at its current generation. Its hash binds ``tasks[].verify``
  and every ``ticket.tasks`` change raises its generation, so an approval never covers a command changed after it; or
- **own**: the caller's own person made the last change to that task's ``verify`` (an unattended agent has no person
  and never satisfies this). An agent author whose grant is now revoked is foreign even to the same person: revoking a
  leaked grant withdraws trust in what it wrote; an expired grant stays trusted (grants expire as a matter of course).

The approval binds the command string only, not the repository content it runs (``make test`` runs whatever the
working copy holds). "Own" means the same person: safe while all of a person's agents share one UID (tier A, direct
mode); tier B must narrow it to the same grant (host §0.8, Phase 3).

Otherwise nothing runs, in direct mode (before the runner), in host mode (the CLI asks before it runs anything) and on
the host when it takes a receipt.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from orch.ops.errors import OrchError

APPROVED, OWN = "approved", "own"


def caller_person(c: Any) -> str | None:
    """The person the caller acts for: the (verified) grant's person, else ``None`` (an unattended session, no-person
    host principal). This is the ``for`` of the caller's agent events."""
    return c.person if c.ctx.grant else None


def grants(c: Any) -> Mapping[str, Any]:
    """The workspace's grants (``GrantView`` by id): an author's grant must not be revoked."""
    return c.store.state.workspace.grants


def _task(view: Any, tid: str) -> Any:
    return next((t for t in view.tasks if t.id == tid), None)


def author(view: Any, tid: str) -> str | None:
    """Who made the last change to the task's ``verify``, for display; ``None`` without one."""
    t = _task(view, tid)
    by: Mapping[str, Any] | None = t.verify_by if t is not None else None
    if by is None:
        return None
    if by["kind"] == "person":
        return f"person {by['id']}"
    if by.get("person") is None:
        return f"unattended agent {by.get('session')}"
    return f"agent {by.get('session')} for {by['person']}"


def _revoked_author(by: Mapping[str, Any], grants: Mapping[str, Any]) -> bool:
    gid = by.get("grant")
    if by["kind"] != "agent":
        return False
    g = grants.get(gid) if gid else None
    return g is None or bool(g.revoked)  # an agent author without a known grant fails closed


def trust(view: Any, tid: str, person: str | None, grants: Mapping[str, Any]) -> str | None:
    """``"approved"``, ``"own"`` or ``None`` (the command must not run for this caller)."""
    plan = view.gates["plan"]
    if plan.applies and plan.approved:
        return APPROVED
    t = _task(view, tid)
    by = t.verify_by if t is not None else None
    if person is not None and by is not None and by.get("person") == person and not _revoked_author(by, grants):
        return OWN
    return None


def require(view: Any, tid: str, person: str | None, grants: Mapping[str, Any]) -> str:
    """:func:`trust`, or ``verify.unapproved`` naming who wrote the command and how to go on."""
    how = trust(view, tid, person, grants)
    if how is not None:
        return how
    who = author(view, tid) or "someone"
    plan = view.gates["plan"]
    if not plan.applies:
        why = "the plan gate does not apply to this ticket"
    elif any(d.kind == "approve" and not d.voided for d in plan.decisions):
        why = "no person approved it in the plan, or the approval is outdated (the plan changed since)"
    else:
        why = "no person approved it in the plan"
    t = _task(view, tid)
    by = t.verify_by if t is not None else None
    if by is not None and person is not None and by.get("person") == person:
        why += "; it was written under a grant that is now revoked"
    raise OrchError(
        "verify.unapproved",
        f"{tid}'s verify command was written by {who} and {why}: nothing was run. Approve the plan "
        f"(a person: orch approve {view.key} plan) or add your own verify command (orch task add TEXT --verify CMD)",
    )
