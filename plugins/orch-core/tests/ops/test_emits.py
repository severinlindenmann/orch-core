"""The frozen operation -> events table (F1 10.1, A5): replay of old logs must not depend on a release's registry."""

from __future__ import annotations

import hashlib
import json

import orch.ops as ops
from orch.model import emits

DIGEST_V1 = "deb64fba742883b0daef32a9e9170991353f139eefad78c4db14089a51970d87"
DIGEST_V2 = "66a7375e81ae5d462aa4246333c0a31fcfedfbbb142fd2fb592cf5029de357f2"
DIGEST_V3 = "da2b8b56cd3cc9fae37737c80c4d23665b0dafb10e90b0fb1acb40610efc950e"
# V3-D38 dropped `init` from all three tables and from HUMAN_ONLY (nothing is in production: no new version)
HUMAN_ONLY_V1 = "8a61d1edd61d03376e26cde56bdd89ceab1bfc9f74a361c11b127163dec888a7"


def digest(table):
    flat = {k: sorted(v) for k, v in sorted(table.items())}
    return hashlib.sha256(json.dumps(flat, separators=(",", ":")).encode()).hexdigest()


def test_v1_is_frozen():
    assert emits.VERSION == 1 and digest(emits.EMITS_V1) == DIGEST_V1  # changing it means a new version, not an edit
    # HUMAN_ONLY decides on replay which verbs grant nothing: it is part of what version 1 means, frozen with the table
    flat = json.dumps(sorted(emits.HUMAN_ONLY), separators=(",", ":")).encode()
    assert hashlib.sha256(flat).hexdigest() == HUMAN_ONLY_V1
    assert emits.EMITS_V1["task.done"] == {"task.done", "artifact.added"}  # a CI grant for task done runs --run
    assert emits.EMITS_V1["handoff"] == {"handoff.written", "claim.released"}
    assert emits.EMITS_V1["log"] == {"log.added"} and "set" in emits.EMITS_V1 and "log.added" not in emits.EMITS_V1


def test_v2_pinned_and_differs_from_v1_only_in_human_only_keys():
    assert digest(emits.EMITS_V2) == DIGEST_V2
    assert {k for k in emits.EMITS_V2 if emits.EMITS_V2[k] != emits.EMITS_V1.get(k)} <= emits.HUMAN_ONLY


def test_v3_adds_only_task_edit_and_the_model_still_reads_v1():
    """R3: ``task.edit`` is a new agent operation. V1 and V2 are untouched, so no replay verdict changes."""
    assert digest(emits.EMITS_V3) == DIGEST_V3 and emits.CURRENT is emits.EMITS_V3
    assert {k for k in emits.EMITS_V3 if emits.EMITS_V3[k] != emits.EMITS_V2.get(k)} == {"task.edit"}
    assert emits.EMITS_V3["task.edit"] == {"ticket.updated"} and "task.edit" not in emits.EMITS_V1
    assert "task.edit" not in ops.verb_events()


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
