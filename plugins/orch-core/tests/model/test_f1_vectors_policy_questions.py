"""Effective-policy cases, question ids, the raise table coverage and the vector index (ticket-format §5.6, §5.7)."""

import json
from pathlib import Path

import pytest

from orch import canon
from orch.model import policies

from .f1_runner import load

DIR = Path(__file__).parent.parent / "vectors" / "f1"


@pytest.mark.parametrize("c", load("effective_policy.json")["cases"], ids=lambda c: c["name"])
def test_effective_policy_cases(c):
    assert policies.intersect(c["workspace"], c["override"]) == c["effective"]


@pytest.mark.parametrize("sc", load("questions.json")["scenarios"], ids=lambda s: s["name"])
def test_question_id_and_hash_of_the_scenarios(sc):
    step = next(s for s in sc["steps"] if s["event"]["type"] == "question.asked" and s["expect"] == "ok")
    ask, uid = step["event"], step["log"]
    q = ask["question"]
    assert canon.question_id(sc["workspace_id"], uid, q["id"]) == sc["qid"] == ask["qid"]
    assert canon.question_hash(ask["qid"], uid, q["text"], q["options"]) == sc["hash"] == ask["hash"]


def test_every_row_of_the_raise_table_has_a_scenario():
    rows = {"ticket.updated", "edit.external", "artifact.added", "artifact.replaced", "task.done", "task.reopened",
            "task.skipped", "branch.pushed", "people.changed", "policy.changed", "addon.granted", "addon.disabled",
            "addon.purged", "member.removed", "role.changed", "device.revoked", "gate.changes_requested",
            "verdict.given", "ticket.reopened", "restore", "gate.approved"}  # fmt: skip
    seen: set[str] = set()
    for f in ("generation.json", "status.json"):
        for sc in load(f)["scenarios"]:
            for st in sc["steps"]:
                e = st["event"]
                if st["expect"] == "ok" and (st.get("raised") or e["type"] in ("verdict.given", "gate.approved")):
                    seen.add(e["type"])
    seen_ticket_restore = any(
        st["event"]["type"] == "restore" and st["log"] != "workspace" and st.get("raised")
        for sc in load("generation.json")["scenarios"] for st in sc["steps"]
    )  # fmt: skip
    assert rows - seen == set(), rows - seen
    assert seen_ticket_restore


def test_the_four_gates_are_each_raised_by_something_and_gate_invalidated_by_nothing():
    raised = set()
    for sc in load("generation.json")["scenarios"]:
        for st in sc["steps"]:
            raised |= set(st.get("raised", []))
            if st["event"]["type"] == "gate.invalidated" and st["expect"] == "ok":
                assert st.get("raised") == [], sc["name"]
    assert raised == {"requirements", "plan", "verify", "code"}


def test_every_vector_file_is_described_in_the_index():
    idx = load("index.json")["files"]
    assert sorted(p.name for p in DIR.glob("*.json")) == sorted(idx)
    for name in idx:
        d = json.loads((DIR / name).read_text(encoding="utf-8"))
        if "[shared" not in idx[name] and name != "index.json":
            assert isinstance(d, dict) and ("pins" in d or "note" in d or "scenarios" in d or "labels" in d), name
