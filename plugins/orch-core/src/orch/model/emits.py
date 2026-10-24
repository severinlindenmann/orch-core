"""The operation -> event types table that grant verbs are judged by, **frozen** (F1 10.1, 10.7, A5).

A grant's ``verbs`` names operations (``task.done``); an agent event is authorized when some granted operation emits its
type. Replay must reach the same verdict for an old log under any later release, so the table is a constant of the
format,
not a read of the live registry: ``EMITS_V1`` is what version 1 means. When an operation's ``emits`` changes (or one is
added), a new table ``EMITS_V2`` is added and :data:`CURRENT` points at it; logs written under version 1 keep replaying
under ``EMITS_V1`` (the version of a grant is the version of the format in force when it was issued: until a second
version exists, all of them are 1). ``tests/ops/test_emits.py`` pins the digest of this table and checks that the live
registry still equals :data:`CURRENT`, so a registry change cannot slip past unnoticed.

Rule (F1 10.1): an event type is covered by ``verbs`` iff it is in the union of the entries of the named operations; a
name that is not a key grants nothing; ``"agent"`` covers every agent operation; a person's event is never covered
(``orch.model.authz.check_actor_kind`` refuses it before verbs are looked at).
"""

from __future__ import annotations

VERSION = 1

EMITS_V1: dict[str, frozenset[str]] = {
    "ac.add": frozenset({"ticket.updated"}),
    "ac.edit": frozenset({"ticket.updated"}),
    "addon.disable": frozenset({"addon.disabled"}),
    "addon.grant": frozenset({"addon.granted"}),
    "addon.purge": frozenset({"addon.purged"}),
    "answer": frozenset({"question.answered"}),
    "apply": frozenset(
        {
            "artifact.added",
            "artifact.replaced",
            "log.added",
            "question.asked",
            "task.blocked",
            "task.done",
            "task.reopened",
            "task.skipped",
            "task.started",
            "ticket.updated",
        }
    ),
    "approve": frozenset({"gate.approved"}),
    "artifact.add": frozenset({"artifact.added"}),
    "artifact.replace": frozenset({"artifact.replaced"}),
    "ask": frozenset({"question.asked"}),
    "claim": frozenset({"claim.taken"}),
    "close": frozenset({"ticket.closed"}),
    "grant": frozenset({"grant.issued"}),
    "grant.revoke": frozenset({"grant.revoked"}),
    "handoff": frozenset({"claim.released", "handoff.written"}),
    "import.v1": frozenset(
        {
            "artifact.added",
            "log.added",
            "question.asked",
            "status.changed",
            "ticket.closed",
            "ticket.created",
            "ticket.updated",
        }
    ),
    "log": frozenset({"log.added"}),
    "member.add": frozenset({"member.added"}),
    "member.remove": frozenset({"member.removed"}),
    "member.role": frozenset({"role.changed"}),
    "new": frozenset({"ticket.created"}),
    "release": frozenset({"claim.released"}),
    "reopen": frozenset({"ticket.reopened"}),
    "request_changes": frozenset({"gate.changes_requested"}),
    "section.set": frozenset({"ticket.updated"}),
    "set": frozenset({"ticket.updated"}),
    "submit": frozenset({"ticket.submitted"}),
    "task.add": frozenset({"ticket.updated"}),
    "task.block": frozenset({"task.blocked"}),
    "task.done": frozenset({"artifact.added", "task.done"}),
    "task.reopen": frozenset({"task.reopened"}),
    "task.skip": frozenset({"task.skipped"}),
    "task.start": frozenset({"task.started"}),
    "verdict": frozenset({"verdict.given"}),
}

HUMAN_ONLY: frozenset[str] = frozenset(
    {
        "addon.disable",
        "addon.grant",
        "addon.purge",
        "answer",
        "approve",
        "close",
        "grant",
        "grant.revoke",
        "import.v1",
        "member.add",
        "member.remove",
        "member.role",
        "reopen",
        "request_changes",
        "verdict",
    }
)
"""The operations a person runs with their own signature (``who: human``). **A verb that names one grants nothing**
(F1 10.1): a model rule, not only a check of ``orch grant``, so a ``grant.issued`` written by another client cannot make
an agent's event covered by ``approve`` or ``import.v1``. ``tests/ops/test_emits.py`` checks the set equals the
registry's human operations and pins its digest next to ``EMITS_V1``: replay judges old logs by it, so changing it
means a new table version, like a change of the emits."""

# B8: differs from V1 only in a HUMAN_ONLY key, so grant-verb verdicts are identical; the model keeps judging by
# EMITS_V1 and VERSION stays 1. ``grant`` (Extend) also emits the ``grant.revoked`` of the runner grant it replaces.
EMITS_V2: dict[str, frozenset[str]] = {**EMITS_V1, "grant": frozenset({"grant.issued", "grant.revoked"})}

# R3: ``task.edit`` (an agent op: one ``ticket.updated`` of ``ticket.tasks``). A NEW key: no EMITS_V1 verdict changes:
# the model keeps judging by V1, where the name ``task.edit`` grants nothing (a grant that wants an agent to edit
# tasks names ``task.add`` or ``agent``, which cover every ``ticket.updated``). Only the registry and person ops move.
EMITS_V3: dict[str, frozenset[str]] = {**EMITS_V2, "task.edit": frozenset({"ticket.updated"})}

CURRENT = EMITS_V3
