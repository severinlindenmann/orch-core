"""The frozen operation -> events table (F1 10.1, A5): replay of old logs must not depend on a release's registry."""

from __future__ import annotations

import hashlib
import json

import orch.ops as ops
from orch.model import emits

DIGEST_V1 = "f9d0697ce185a381c789a7bd3ad40fbf1ccc7c1b16b880fe6d15e0bd6f58bfca"


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
