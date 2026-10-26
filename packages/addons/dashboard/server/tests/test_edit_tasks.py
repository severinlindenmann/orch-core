"""Task edits keep the reviewed snapshot and use core's real sequence validator."""

import pytest
from orch.host import sequence
from pydantic import ValidationError

from orch_dashboard.errors import HostError
from orch_dashboard.handoff import EditTicket


@pytest.fixture
def data():
    return {
        "doc": {
            "key": "DEMO-0001",
            "uid": "01AAAAAAAAAAAAAAAAAAAAAAAA",
            "type": "feature",
            "head": {"seq": 7, "head": "sha256:" + "a" * 64},
            "sections": {"summary": "Before"},
            "acceptance": [{"id": "AC1", "text": "It opens", "state": "unproven"}],
            "tasks": [
                {
                    "id": "T1",
                    "text": "Open it",
                    "verify": {"cmd": "check"},
                    "proves": ["AC1"],
                    "assignee": "p_" + "a" * 32,
                    "state": "todo",
                    "receipt": None,
                }
            ],
        }
    }


def intent(data, tasks, **kwargs):
    return EditTicket.model_validate(
        {"action": "edit_sequence", "key": "DEMO-0001", "head_seq": 7, "tasks": tasks, **kwargs}
    ).intent(data)


def test_projection_and_acceptance_snapshot_produce_only_task_steps(data):
    made = intent(data, {"edit": {"T1": {"text": "Open again", "verify": "check", "proves": ["AC1"]}}})
    assert made["tasks"]["was"] == [{k: v for k, v in data["doc"]["tasks"][0].items() if k not in {"state", "receipt"}}]
    assert made["tasks"]["edit"] == {"T1": {"text": "Open again"}}
    assert made["acceptance"] == {"was": [{"id": "AC1", "text": "It opens"}], "edit": {}, "add": []}
    assert [s["op"] for s in sequence.steps(made)] == ["task.edit"]


def test_null_removes_verify_but_missing_leaves_it_alone(data):
    assert intent(data, {"edit": {"T1": {"verify": None}}})["tasks"]["edit"] == {"T1": {"verify": None}}
    assert intent(data, {"edit": {"T1": {"text": "New text"}}})["tasks"]["edit"] == {"T1": {"text": "New text"}}


def test_noops_and_blank_adds_disappear(data):
    made = intent(
        data,
        {
            "edit": {"T1": {"text": "Open it", "verify": "check", "proves": ["AC1"]}},
            "add": [{"text": "  ", "proves": []}, {"text": "", "proves": []}],
        },
        sections={"summary": "After"},
    )
    assert "tasks" not in made and "acceptance" not in made
    with pytest.raises(HostError):
        intent(data, {"edit": {"T1": {}}})


def test_new_criterion_can_be_proven_in_same_edit(data):
    made = intent(data, {"add": [{"text": "Save", "proves": ["AC2"]}]}, acceptance={"add": ["It saves"]})
    assert [s["op"] for s in sequence.steps(made)] == ["ac.add", "task.add"]


@pytest.mark.parametrize(
    "change",
    [
        {"edit": {"T2": {"text": "Unknown"}}},
        {"edit": {"T1": {"proves": ["AC9"]}}},
        {"edit": {"T1": {"proves": ["AC1", "AC1"]}}},
        {"edit": {"T1": {"text": ""}}},
        {"edit": {"T1": {"verify": "é" * 3000}}},
    ],
)
def test_invalid_task_changes_fail(data, change):
    with pytest.raises(HostError):
        intent(data, change)


def test_done_task_only_allows_text(data):
    data["doc"]["tasks"][0]["state"] = "done"
    assert intent(data, {"edit": {"T1": {"text": "Clearer"}}})["tasks"]["edit"] == {"T1": {"text": "Clearer"}}
    for change in ({"verify": None}, {"proves": []}):
        with pytest.raises(HostError):
            intent(data, {"edit": {"T1": change}})


@pytest.mark.parametrize(
    "change",
    [
        {"edit": {"T1": {"extra": True}}},
        {"edit": {"T1": {"text": 42}}},
        {"add": [{"text": "New", "proves": [], "verify": None}]},
        {"edit": {"T1": {"text": "New"}}, "add": [{"text": "New", "proves": []}] * 32},
    ],
)
def test_strict_schema_and_combined_limit(data, change):
    with pytest.raises(ValidationError):
        intent(data, change)


def test_sequence_step_limit_is_not_weakened(data):
    with pytest.raises(HostError):
        intent(data, {"add": [{"text": "New", "proves": []}] * 17})


def test_task_text_is_normalized_before_comparing(data):
    data["doc"]["tasks"][0]["text"] = "Café"
    made = intent(data, {"edit": {"T1": {"text": "Cafe\u0301"}}, "add": [{"text": "Cafe\u0301", "proves": []}]})
    assert made["tasks"]["edit"] == {}
    assert made["tasks"]["add"] == [{"text": "Café", "proves": []}]
