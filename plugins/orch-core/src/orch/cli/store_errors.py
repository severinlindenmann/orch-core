"""One mapping from what the store raises (:class:`~orch.store.StoreError`) to the CLI's error catalog (ticket-format
§10.4, ``orch.ops.errors``).

A model refusal keeps the meaning of its code but speaks the CLI's vocabulary (``gate.stale`` stays,
``status.transition`` becomes ``transition.refused``, ``ticket.unknown`` ``not_found``...). A code that the calling
operation does not declare (and that is not a global error) falls back to ``invalid.input`` for a refusal and
``internal`` for a store or chain fault, so ``cli.main.run`` never sees an undeclared code. ``store.busy`` is
``retry.later``.
"""

from __future__ import annotations

from collections.abc import Collection

from orch.ops.errors import GLOBAL_ERRORS, OrchError
from orch.store import StoreError

__all__ = ["TABLE", "to_orch_error"]

_INTERNAL = (
    "chain.broken chain.diverged chain.bad_ws_seq trust.genesis_mismatch genesis.invalid auth.invalid_event "
    "event.bad_base event.duplicate_id event.bad_actor event.unknown_type store.torn_write store.read_only "
    "restore.bad_head ack.unknown"
).split()
_REFUSED = (  # an event the model refused: the request is what is wrong
    "sig.invalid freeze.active member.unknown member.exists members.last_owner device.unknown device.exists "
    "device.invalid device.scope device.cert grant.terms grant.exists grant.unknown unattended.denied "
    "settings.invalid addon.unknown ticket.exists ticket.bad_reference submit.incomplete people.invalid "
    "path.protected body.unknown_section body.unknown_artifact repo.unknown gate.no_eligible gate.incomplete "
    "gate.not_applicable gate.invalidated_mismatch policy.invalid source.unlinked source.not_new task.unknown "
    "task.bad_receipt artifact.exists artifact.unknown artifact.bad_replaces artifact.kind question.unknown "
    "question.stale question.answered question.bad_id question.bad_hash question.reask answer.bad_option "
    "body.bad_refs"
).split()

TABLE: dict[str, str] = {
    **dict.fromkeys(_INTERNAL, "internal"),
    **dict.fromkeys(_REFUSED, "invalid.input"),
    "store.busy": "retry.later",
    "members.stale": "members.stale",
    "quota.unattended": "quota.unattended",
    "gate.stale": "gate.stale",
    "conflict.section": "conflict.section",
    "source.missing": "source.missing",
    "claim.not_live": "claim.not_live",
    "role.denied": "role.denied",
    "answer.not_allowed": "role.denied",
    "gate.not_eligible": "role.denied",
    "grant.invalid": "grant.expired",
    "grant.scope": "grant.expired",
    "grant.verb": "grant.verb",
    "ticket.unknown": "not_found",
    "ticket.not_visible": "not_found",
    "ticket.frozen": "transition.refused",
    "status.transition": "transition.refused",
    "gate.status": "transition.refused",
    "task.state": "transition.refused",
    "ticket.state": "transition.refused",
    "claim.exists": "claim.held",
    "claim.not_holder": "claim.held",
    "task.leased": "lease.held",
}


def code_for(code: str) -> str:
    """The catalog code for a store or model code: the table, ``invalid.input`` for ``validation.*`` and ``body.*``,
    ``internal`` for anything it does not know."""
    if code in TABLE:
        return TABLE[code]
    if code.startswith(("validation.", "body.")):
        return "invalid.input"
    return "internal"


def to_orch_error(e: StoreError, declared: Collection[str] = ()) -> OrchError:
    """``e`` as an :class:`OrchError` the operation may raise: its mapped code if the operation declares it or it is a
    global error; else ``invalid.input`` for a refusal and ``internal`` for a fault."""
    mapped = code_for(e.code)
    if mapped not in GLOBAL_ERRORS and mapped not in declared:
        mapped = "internal" if code_for(e.code) == "internal" else "invalid.input"
    detail = f"{e.code}: {e.detail}" if e.detail else e.code
    return OrchError(mapped, detail[:200])
