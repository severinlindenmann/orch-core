"""The frozen operation -> events table (F1 10.1, A5): replay of old logs must not depend on a release's registry."""

from __future__ import annotations

import hashlib
import json

import orch.ops as ops
from orch.model import emits

DIGEST_V1 = "dc086fd5ad0dee3afff93646a3d4f9a0369d1be6037ef969e2d74b36131bfe8a"


def digest(table):
    flat = {k: sorted(v) for k, v in sorted(table.items())}
    return hashlib.sha256(json.dumps(flat, separators=(",", ":")).encode()).hexdigest()


def test_v1_is_frozen():
    assert emits.VERSION == 1 and digest(emits.EMITS_V1) == DIGEST_V1  # changing it means a new version, not an edit
    assert emits.EMITS_V1["task.done"] == {"task.done", "artifact.added"}  # a CI grant for task done runs --run
    assert emits.EMITS_V1["handoff"] == {"handoff.written", "claim.released"}
    assert emits.EMITS_V1["log"] == {"log.added"} and "set" in emits.EMITS_V1 and "log.added" not in emits.EMITS_V1


def test_the_live_registry_matches_the_current_table():
    assert ops.registry_emits() == dict(emits.CURRENT), (
        "an operation's emits changed: add EMITS_V2 (keep EMITS_V1 for old logs), point CURRENT at it, and update "
        "the digest"
    )


def test_the_model_reads_the_frozen_table_not_the_registry(monkeypatch):
    monkeypatch.setattr(ops, "registry_emits", lambda: {})
    assert ops.verb_events() == dict(emits.EMITS_V1)


def test_an_operation_a_person_runs_is_never_covered_by_a_grant():
    """F1 10.1 as a model rule: a verb naming a human operation grants nothing, whatever a ``grant.issued`` says."""
    from orch.model.authz import verb_covers

    human = {op.name for op in ops.all() if op.who == "human"}
    assert emits.HUMAN_ONLY == human
    table = dict(emits.CURRENT)
    assert verb_covers("ticket.created", ["new"], table) and verb_covers("log.added", ["log"], table)
    for verb, typ in (("import.v1", "ticket.updated"), ("import.v1", "ticket.created"), ("approve", "gate.approved")):
        assert not verb_covers(typ, [verb], table)
    assert verb_covers("log.added", ["import.v1", "log"], table)  # the agent verb next to it still counts
