"""R11 P09: the typed action approve_both builds one closed intent from the host's own reads (ticket + gate.input)."""

import copy

import pytest
from orch import canon
from orch.host import sequence

from orch_dashboard.errors import HostError
from orch_dashboard.signing import ApproveBoth, host_call  # noqa: F401

REQ = "Write hello.txt."
PLAN = "Do it."
AC = [{"id": "AC1", "text": "hello.txt exists"}]
TASKS = [{"id": "T1", "text": "Write the file", "verify": {"cmd": "test -f hello.txt"}, "proves": ["AC1"]}]


def _gate(gate, sections, tasks):
    return {
        "input": {
            "gate": gate,
            "sections": {sid: canon.section_hash(t) for sid, t in sections.items()},
            "tasks": tasks,
            "fields": {"acceptance": AC, "ticket_type": "chore", "size": "s"},
        }
    }


@pytest.fixture
def reads():
    data = {
        "doc": {
            "key": "DEMO-0001",
            "uid": "01AAAAAAAAAAAAAAAAAAAAAAAA",
            "type": "chore",
            "title": "Add a hello.txt",
            "head": {"seq": 7, "head": "sha256:" + "a" * 64},
            "sections": {"summary": "S", "context": "C", "requirements": REQ, "plan": PLAN},
        }
    }
    inputs = {
        "requirements": _gate("requirements", {"summary": "S", "context": "C", "requirements": REQ}, []),
        "plan": _gate("plan", {"plan": PLAN, "decisions": ""}, TASKS),
    }
    return data, inputs


def made(reads, **kw):
    data, inputs = reads
    body = {"action": "approve_both", "key": "DEMO-0001", "head_seq": 7, **kw}
    return ApproveBoth.model_validate(body).intent(data, inputs)


def test_the_intent_carries_both_full_texts_and_two_approve_steps(reads):
    intent = made(reads)
    assert intent["size"] == "s"  # the size is part of the reviewed intent
    assert intent["kind"] == "approve_gates" and intent["head"] == "sha256:" + "a" * 64
    assert intent["requirements"]["sections"]["requirements"] == REQ and intent["plan"]["sections"]["plan"] == PLAN
    assert [(s["op"], s["args"]) for s in sequence.steps(intent)] == [
        ("approve", {"gate": "requirements"}),
        ("approve", {"gate": "plan"}),
    ]
    assert sequence.step_args(intent, 0, None) == ("approve", {"ref": "DEMO-0001", "gate": "requirements"})


def test_a_ticket_that_moved_since_the_page_read_it_is_refused(reads):
    with pytest.raises(HostError) as e:
        made(reads, head_seq=6)
    assert "conflict.stale" in str(e.value)


def test_a_text_that_is_not_the_one_the_gate_binds_is_refused(reads):
    data, inputs = copy.deepcopy(reads)
    data["doc"]["sections"]["plan"] = "Do something else."
    with pytest.raises(HostError):
        ApproveBoth.model_validate({"action": "approve_both", "key": "DEMO-0001", "head_seq": 7}).intent(data, inputs)


def test_extra_fields_and_other_keys_are_refused_by_the_model(reads):
    with pytest.raises(ValueError):
        ApproveBoth.model_validate({"action": "approve_both", "key": "DEMO-0001", "head_seq": 7, "gate": "plan"})
    with pytest.raises(HostError):
        made(reads, key="DEMO-0002")
