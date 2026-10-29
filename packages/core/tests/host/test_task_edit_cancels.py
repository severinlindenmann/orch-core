"""R3 part A1: the person's ``task.edit`` (one signed ``ticket.updated`` of ``ticket.tasks``, bound by the signer), the
plain review of a task change (before -> after) and the host's "approvals this edit cancels" preview
(``prepared["cancels"]``: the model's own ``settle`` on a scratch copy; the host's word, never signed)."""

from __future__ import annotations

import copy

import pytest

from orch import canon
from orch.host import cancels, sequence, signcontent
from tests.host import human_helpers
from tests.host.human_helpers import ok
from tests.host.test_human import logged, ready
from tests.host.test_human_fixes import refused, reseal
from tests.host.test_sequence import Rig
from tests.host.test_sequence_edit import edit_intent

lab, served, human = human_helpers.lab, human_helpers.served, human_helpers.human


def with_task(human, key, **kw):
    human.run("task.add", {"ref": key, "text": "write it", "verify": "make test", "proves": ["AC1"], **kw})


def approved_requirements(human, key):
    ready(human, key)
    human.run("approve", {"ref": key, "gate": "requirements"})


# --- the person op ------------------------------------------------------------------------------------------------


def test_a_person_signs_a_task_edit_and_only_the_task_changes(human, served):
    _, key = human.ticket()
    human.run("ac.add", {"ref": key, "text": "it works"})
    with_task(human, key)
    prepared, _, signed = human.run(
        "task.edit", {"ref": key, "task": "T1", "text": "write it well", "verify": "make check"}
    )
    (ev,) = prepared["events"]
    assert (
        ev["type"] == "ticket.updated"
        and set(ev["set"]) == {"ticket.tasks"}
        and set(ev["base_rev"]) == {"ticket.tasks"}
    )
    last = logged(served, key)[-1]
    assert last["type"] == "ticket.updated" and last["actor"]["kind"] == "person"
    (t,) = served.lab.ws.ticket_json(key)["tasks"]
    assert t == {"id": "T1", "text": "write it well", "verify": {"cmd": "make check"}, "proves": ["AC1"]}


def test_the_signer_binds_task_edit_to_the_task_list(human, served):
    _, key = human.ticket()
    human.run("ac.add", {"ref": key, "text": "it works"})
    with_task(human, key)
    p = human.prepare("task.edit", {"ref": key, "task": "T1", "text": "other words"})
    bad = copy.deepcopy(p)
    ev = bad["events"][0]
    ev["set"] = {"ticket.acceptance": [{"id": "AC1", "text": "weaker"}]}  # a task.edit that rewrites a criterion
    ev["base_rev"] = {"ticket.acceptance": ev["base_rev"].pop("ticket.tasks")}
    bad["material"].pop("before")
    refused(human, reseal(bad), "does not change")
    bad = copy.deepcopy(p)
    bad["events"][0]["set"]["ticket.title"] = "also the title"  # ... or the title
    refused(human, reseal(bad), "base_rev|does not change")


def test_a_done_tasks_command_and_criteria_cannot_be_edited_by_a_person_either(human, served):
    served.lab.ws.plan_gate_off()  # r10-P02: no plan approval before the build in this test
    _, key = human.ticket()
    human.run("ac.add", {"ref": key, "text": "first"})
    human.run("ac.add", {"ref": key, "text": "second"})
    with_task(human, key, verify="true")
    # an agent session claims the ticket and finishes the task by hand
    human.agent("claim", {"ref": key})
    human.agent("task.done", {"task": f"{key}/T1", "message": "by hand"})
    for args in ({"verify": "false"}, {"proves": ["AC2"]}, {"proves": []}):
        res = human.prepare_res("task.edit", {"ref": key, "task": "T1", **args})
        assert res["error"]["code"] == "transition.refused", args
    ok(human.prepare_res("task.edit", {"ref": key, "task": "T1", "text": "reworded"}))  # the text may change


def test_the_review_says_what_changed_in_the_plan_before_and_after(human, served):
    _, key = human.ticket()
    human.run("ac.add", {"ref": key, "text": "it works"})
    with_task(human, key)
    p = human.prepare("task.edit", {"ref": key, "task": "T1", "text": "write it well", "proves": []})
    human.check(p)
    content = signcontent.build(p, verified=True)
    (row,) = content["fields_changed"]
    assert row["field"] == "Task T1" and row.get("before_note") is None  # before is checked against base_rev
    assert row["before"] == "write it · verify: make test · proves AC1"
    assert row["after"] == "write it well · verify: make test"
    assert [t["id"] for t in content["tasks"]] == ["T1"]  # the whole new plan is still shown


def test_an_added_and_a_removed_task_are_rows_too():
    rows: dict = {}
    old = [
        {"id": "T1", "text": "a", "verify": None, "proves": []},
        {"id": "T2", "text": "b", "verify": None, "proves": []},
    ]
    new = [
        {"id": "T1", "text": "a", "verify": None, "proves": []},
        {"id": "T3", "text": "c", "verify": {"cmd": "x"}, "proves": []},
    ]
    signcontent._task_rows(rows, new, old, None)
    got = {r["field"]: (r["before"], r["after"]) for r in rows["fields_changed"]}
    assert got == {"Task T2": ("b", "(removed)"), "Task T3": (None, "c · verify: x")}


# --- the cancel preview -------------------------------------------------------------------------------------------


def test_an_edit_that_cancels_an_approval_lists_it(human, served):
    _, key = human.ticket()
    approved_requirements(human, key)
    p = human.prepare("ac.edit", {"ref": key, "ac": "AC1", "text": "it works better"})
    assert p["cancels"] == [{"gate": "requirements", "people": [human.person.ref], "complete": True}]
    human.check(p)
    content = signcontent.build(p, verified=True)
    assert content["cancels"] == p["cancels"]
    notes = content["notes"]
    assert any(n.startswith("This cancels any existing approval of: requirements") for n in notes)  # the signed note
    host_note = next(n for n in notes if n.startswith("Per the host (not signed)"))
    assert "requirements (1 person)" in host_note
    lines = signcontent.text(content, lambda s: s, members=[{"person": human.person.ref, "name": "Severin"}])
    assert "Approvals this cancels (per host, not signed)" in lines
    assert any(line.startswith("  requirements: ") and "Severin" in line for line in lines)


def test_an_edit_that_cancels_nothing_says_so_and_a_title_edit_is_silent(human, served):
    _, key = human.ticket()
    approved_requirements(human, key)
    p = human.prepare(
        "task.add", {"ref": key, "text": "later"}
    )  # the plan is bound by plan/verify, not by requirements
    assert p["cancels"] == []
    notes = signcontent.build(p, verified=True)["notes"]
    assert "Per the host (not signed): no approval stands right now, so this cancels none." in notes
    t = human.prepare("set", {"ref": key, "pairs": ["priority=high"]})
    assert t["cancels"] == []  # priority is bound by no gate
    assert not any("Per the host" in n for n in signcontent.build(t, verified=True).get("notes", []))


def test_a_request_without_a_ticket_write_carries_no_preview(human, served):
    _, key = human.ticket()
    assert "cancels" not in human.prepare("log", {"ref": key, "text": "a note"})
    assert "cancels" not in human.prepare("new", {"title": "Another"})


def test_the_preview_is_the_models_own_settle_not_a_second_table(human, served):
    """The scratch copy leaves the real ticket alone: preparing never changes what stands."""
    _, key = human.ticket()
    approved_requirements(human, key)
    before = served.lab.ws.ticket_json(key)
    for _ in range(2):
        assert human.prepare("ac.edit", {"ref": key, "ac": "AC1", "text": "x"})["cancels"][0]["gate"] == "requirements"
    assert served.lab.ws.ticket_json(key) == before


def test_an_edit_sequence_previews_every_step_once(human, tmp_path):
    _, key = human.ticket()
    approved_requirements(human, key)
    intent = edit_intent(
        human,
        key,
        sections={"context": {"was": canon.section_hash("the context"), "text": "a new context"}},
        acceptance={"was": [{"id": "AC1", "text": "it works"}], "edit": {"AC1": "it works well"}, "add": ["and more"]},
    )
    rig = Rig(human, tmp_path / "edit", intent)
    first = rig.first()
    assert first["cancels"] == [{"gate": "requirements", "people": [human.person.ref], "complete": True}]
    human.check(first)
    assert signcontent.build(first, verified=True)["cancels"] == first["cancels"]
    # later steps carry no preview of their own: the first review counted them all
    out = rig.driver().run(first)
    assert out["landed"] >= 2
    assert all("cancels" not in p for p in rig.prepared)


def test_the_paths_of_events_and_of_an_intent():
    ev = [
        {"type": "ticket.updated", "set": {"ticket.tasks": []}, "sections": {"plan": {}}},
        {"type": "log.added", "text": "x"},
    ]
    assert cancels.paths_of_events(ev) == {"ticket.tasks", "body.plan"}
    assert cancels.paths_of_events([{"type": "people.changed"}]) == set()
    intent = sequence.validate_intent(
        {
            "v": 1,
            "kind": "edit_ticket",
            "ticket": {"key": "DEMO-0001", "uid": "0" * 26},
            "ticket_type": "feature",
            "head": "sha256:" + "0" * 64,
            "sections": {"summary": {"was": canon.section_hash(""), "text": "x"}},
            "acceptance": {"was": [], "edit": {}, "add": ["c"]},
        }
    )
    assert cancels.paths_of_intent(intent, "p_" + "1" * 32) == {"body.summary", "ticket.acceptance"}


@pytest.mark.parametrize(
    "bad",
    [
        "requirements",
        [{"gate": "nope", "people": ["p_" + "1" * 32], "complete": True}],
        [{"gate": "plan", "people": [], "complete": True}],
        [{"gate": "plan", "people": ["not a person"], "complete": True}],
        [{"gate": "plan", "people": ["p_" + "1" * 32], "complete": "yes"}],
        [{"gate": "plan", "people": ["p_" + "1" * 32], "complete": True, "extra": "text"}],
        [
            {"gate": "plan", "people": ["p_" + "1" * 32], "complete": True},
            {"gate": "requirements", "people": ["p_" + "1" * 32], "complete": True},
        ],
        [
            {"gate": "plan", "people": ["p_" + "1" * 32], "complete": True},
            {"gate": "plan", "people": ["p_" + "2" * 32], "complete": True},
        ],
    ],
)
def test_a_malformed_preview_never_reaches_the_review(human, served, bad):
    _, key = human.ticket()
    p = human.prepare("ac.add", {"ref": key, "text": "x"})
    p["cancels"] = bad
    with pytest.raises(signcontent.PlainUnavailable):
        signcontent.build(p, verified=True)
