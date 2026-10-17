"""B12D (finding N3): acceptance criteria and people in the New ticket sequence, and an inline edit of an existing
ticket as ONE review (the same engine; every step is checked against the reviewed intent)."""

from __future__ import annotations

import pytest

from orch import canon
from orch.host import sequence, signcontent, signer
from orch.host.sequence import Aborted, SequenceInvalid
from tests.host import human_helpers
from tests.host.human_helpers import ok
from tests.host.test_sequence import Rig, doc, intent_of

lab, served, human = human_helpers.lab, human_helpers.served, human_helpers.human

pytestmark = pytest.mark.slow  # a host


def test_a_new_ticket_with_criteria_and_people_lands_in_one_run(human, lab, tmp_path):
    quinn = lab.add_member()
    rig = Rig(
        human,
        tmp_path,
        intent_of(
            acceptance=["It opens", "It saves"],
            people={"assignees": [quinn.ref], "reviewers": [human.person.ref], "owner": quinn.ref},
        ),
    )
    first = rig.first()
    human.check(first)
    out = rig.driver().run(first)
    assert out["landed"] == 4 + 2 + 3  # new, set, two sections, two criteria, three people changes
    got = doc(human, out["key"])
    assert [a["text"] for a in got["doc"]["acceptance"]] == ["It opens", "It saves"]
    assert [a["id"] for a in got["doc"]["acceptance"]] == ["AC1", "AC2"]
    assert got["doc"]["people"]["assignees"] == [quinn.ref]
    assert got["doc"]["people"]["reviewers"] == [human.person.ref]
    assert got["doc"]["owner"] == quinn.ref  # the owner is set LAST


def test_the_review_shows_criteria_and_people_and_needs_the_typed_word(human, lab, tmp_path):
    quinn = lab.add_member()
    rig = Rig(human, tmp_path, intent_of(acceptance=["It opens"], people={"assignees": [quinn.ref]}))
    first = rig.first()
    human.check(first)
    content = signcontent.build(first, verified=True)
    assert [a["text"] for a in content["acceptance"]] == ["It opens"]
    assert any(f["field"] == "assignees: add" and f["after"] == quinn.ref for f in content["fields_changed"])
    assert content["subline"] == "and its text (2 sections), 1 acceptance check, its people"
    assert sequence.confirm_kind(rig.intent) == "word"  # people.set decides who may act: the strongest step
    assert "It opens" in signer.render(first, lambda w: ("ws", "ORCH"))


def test_an_older_intent_keeps_its_digest():
    old = sequence.validate_intent(intent_of())
    assert "acceptance" not in old and "people" not in old
    with_ac = sequence.validate_intent(intent_of(acceptance=["x"]))
    assert sequence.digest_of("a" * 32, "p_" + "1" * 32, old) != sequence.digest_of("a" * 32, "p_" + "1" * 32, with_ac)


@pytest.mark.parametrize(
    "bad",
    [
        {"acceptance": []},
        {"acceptance": ["a\nb"]},
        {"acceptance": ["x"] * 9},
        {"people": {}},
        {"people": {"watchers": ["p_" + "1" * 32]}},
        {"people": {"assignees": ["p_" + "2" * 32, "p_" + "1" * 32]}},
        {"people": {"owner": ["p_" + "1" * 32]}},
        {"people": {"assignees": ["not-a-person"]}},
        {"unknown": 1},
    ],
)
def test_an_unknown_or_malformed_key_is_refused(bad):
    with pytest.raises(SequenceInvalid):
        sequence.validate_intent(intent_of(**bad))


def test_an_unknown_key_falls_back_to_the_full_review(human, tmp_path):
    rig = Rig(human, tmp_path)
    first = rig.first()
    first["material"]["sequence"]["intent"]["future_key"] = 1  # an intent this build does not know
    with pytest.raises(signcontent.PlainUnavailable):
        signcontent.build(first, verified=True)  # the plain view says so (the caller shows the full review) ...
    with pytest.raises(signer.SignerRefused):
        human.check(first)  # ... and the signer signs nothing it cannot reproduce


def test_a_foreign_event_in_the_new_ticket_sequence_aborts(human, lab, tmp_path):
    quinn = lab.add_member()
    rig = Rig(human, tmp_path, intent_of(acceptance=["It opens"], people={"assignees": [quinn.ref]}))
    first = rig.first()
    real = rig.prepare

    def prepare(op, args, meta):
        if meta["step"] == 4:  # the first criterion: someone else writes to the ticket before it is prepared
            human.agent("log", {"ref": args["ref"], "message": "foreign"})
        return real(op, args, meta)

    rig.port.prepare = prepare
    with pytest.raises(Aborted):
        rig.driver().run(first)
    assert len(rig.submitted) == 4  # the ticket and its text exist: a visible draft, no criteria, no people
    key = rig.sessions.get(rig.wid, rig.driver().digest)["ticket"]["key"]
    assert doc(human, key)["doc"]["acceptance"] == []


def edit_intent(human, key, **over):
    """The edit intent of ``key`` the way the dashboard builds it from what the page showed."""
    data = ok(human.c.req("ticket.get", {"ref": key}))["data"]
    base = {
        "v": 1,
        "kind": "edit_ticket",
        "ticket": {"key": key, "uid": data["doc"]["uid"]},
        "ticket_type": data["doc"]["type"],
        "head": data["doc"]["head"]["head"],
        "sections": {},
    }
    return {**base, **over}


def made(human, tmp_path, **kw):
    """A ticket created by a plain sequence, with one criterion."""
    rig = Rig(human, tmp_path, intent_of(acceptance=["First check"], **kw))
    out = rig.driver().run(rig.first())
    return out["key"]


def test_an_inline_edit_signs_sections_criteria_and_people_as_one_review(human, lab, tmp_path):
    quinn = lab.add_member()
    key = made(human, tmp_path)
    data = doc(human, key)
    was = canon.section_hash("One line.")
    intent = edit_intent(
        human,
        key,
        sections={"summary": {"was": was, "text": "A better line."}},
        acceptance={
            "was": [{"id": "AC1", "text": "First check"}],
            "edit": {"AC1": "First check, reworded"},
            "add": ["Second check"],
        },
        people={"assignees": {"add": [quinn.ref], "remove": []}},
    )
    rig = Rig(human, tmp_path / "edit", intent)
    first = rig.first()
    human.check(first)
    content = signcontent.build(first, verified=True)
    assert content["headline"].startswith("Edit ")
    assert [a["text"] for a in content["acceptance"]] == ["First check, reworded", "Second check"]
    assert any(s["id"] == "summary" and s["text"] == "A better line." for s in content["sections"])
    out = rig.driver().run(first)
    assert out["landed"] == 4 and out["key"] == key
    got = doc(human, key)
    assert [a["text"] for a in got["doc"]["acceptance"]] == ["First check, reworded", "Second check"]
    assert got["doc"]["people"]["assignees"] == [quinn.ref]
    assert got["doc"]["head"]["seq"] == data["doc"]["head"]["seq"] + 4


def test_an_edit_based_on_an_old_head_is_refused_by_the_host(human, tmp_path):
    key = made(human, tmp_path)
    intent = edit_intent(human, key, sections={"summary": {"was": canon.section_hash("One line."), "text": "New."}})
    human.agent("log", {"ref": key, "message": "moved on"})  # a foreign event after the page was read
    rig = Rig(human, tmp_path / "edit", intent)
    op, args = sequence.step_args(rig.intent, 0, None)
    res = human.c.req("human.prepare", {"op": op, "args": args, "signer": human.signer, "sequence": rig.meta0()})
    assert res["error"]["code"] == "human.request_mismatch"


def test_a_foreign_event_during_an_edit_aborts(human, lab, tmp_path):
    quinn = lab.add_member()
    key = made(human, tmp_path)
    intent = edit_intent(
        human,
        key,
        sections={"summary": {"was": canon.section_hash("One line."), "text": "New."}},
        people={"assignees": {"add": [quinn.ref], "remove": []}},
    )
    rig = Rig(human, tmp_path / "edit", intent)
    first = rig.first()

    real = rig.prepare

    def prepare(op, args, meta):
        if meta["step"] == 1:  # someone else writes between the section and the people change
            human.agent("log", {"ref": key, "message": "foreign"})
        return real(op, args, meta)

    rig.port.prepare = prepare
    with pytest.raises(Aborted) as exc:
        rig.driver().run(first)
    assert exc.value.landed == 1 and exc.value.total == 2  # the host refused the chain (the harness raises)
    assert doc(human, key)["doc"]["people"]["assignees"] == []


def test_an_edit_step_that_differs_from_the_intent_aborts_before_the_signature(human, tmp_path):
    key = made(human, tmp_path)
    intent = edit_intent(
        human,
        key,
        sections={"summary": {"was": canon.section_hash("One line."), "text": "New."}},
        acceptance={"was": [{"id": "AC1", "text": "First check"}], "edit": {}, "add": ["Second"]},
    )
    rig = Rig(human, tmp_path / "edit", intent)
    first = rig.first()
    real = rig.prepare

    def evil(op, args, meta):
        return real(op, {**args, "text": args["text"] + " (host)"}, meta)

    rig.port.prepare = evil
    with pytest.raises(Aborted):
        rig.driver().run(first)
    assert [a["text"] for a in doc(human, key)["doc"]["acceptance"]] == ["First check"]


@pytest.mark.parametrize(
    "bad",
    [
        {},  # nothing changes
        {"sections": {"summary": {"was": "sha256:" + "0" * 64, "text": ""}}},
        {"acceptance": {"was": [], "edit": {"AC9": "x"}, "add": []}},
        {"people": {"owner": {"add": ["p_" + "1" * 32, "p_" + "2" * 32], "remove": []}}},
        {"people": {"assignees": {"add": ["p_" + "1" * 32], "remove": ["p_" + "1" * 32]}}},
        {"unknown": 1},
    ],
)
def test_a_malformed_edit_is_refused(bad):
    intent = {
        "v": 1,
        "kind": "edit_ticket",
        "ticket": {"key": "DEMO-0001", "uid": "0" * 26},
        "ticket_type": "feature",
        "head": "sha256:" + "0" * 64,
        "sections": {},
        **bad,
    }
    with pytest.raises(SequenceInvalid):
        sequence.validate_intent(intent)


def test_a_person_who_is_not_a_member_cannot_be_added(human, tmp_path):
    key = made(human, tmp_path)
    res = human.prepare_res("people.set", {"ref": key, "role": "assignees", "add": ["p_" + "9" * 32], "remove": []})
    assert res["error"]["code"] == "invalid.input"


def test_a_foreign_event_between_the_edit_prepare_and_submit_aborts(human, tmp_path):
    """Opus M1: the edit's first step is re-checked against the head under the lock at submit."""
    key = made(human, tmp_path)
    intent = edit_intent(human, key, sections={"summary": {"was": canon.section_hash("One line."), "text": "New."}})
    rig = Rig(human, tmp_path / "edit", intent)
    first = rig.first()
    human.check(first)
    signed = human.sign(first)
    human.agent("log", {"ref": key, "message": "foreign"})  # after the prepare, before the submit
    res = human.submit_res(first["request"]["id"], signed.events)
    assert res["error"]["code"] == "human.request_mismatch"
    assert doc(human, key)["doc"]["sections"]["summary"] == "One line."


def test_an_edit_that_changes_people_is_refused_for_a_signer_who_may_not_manage(human, lab, tmp_path):
    key = made(human, tmp_path)
    quinn = lab.add_member()
    intent = edit_intent(human, key, people={"assignees": {"add": [quinn.ref], "remove": []}})
    rig = Rig(human, tmp_path / "edit", intent)
    # the owner may: prepared; (a non-managing signer is refused at the same prepare, see people.set tests)
    assert "error" not in human.c.req(
        "human.prepare",
        {
            "op": "people.set",
            "args": sequence.step_args(rig.intent, 0, None)[1],
            "signer": human.signer,
            "sequence": rig.meta0(),
        },
    )
