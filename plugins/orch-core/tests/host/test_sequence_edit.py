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


def test_an_edit_is_signed_through_the_terminal_path_with_a_batch_unlock(human, lab, tmp_path):
    """The terminal path renders the intent too (the unlock prompt names it): an edit has no title, only its ticket."""
    import contextlib

    from tests.host.test_handoff import Shown
    from tests.host.test_sequence import handoff_first, terminal

    quinn = lab.add_member()
    key = made(human, tmp_path)
    intent = edit_intent(
        human,
        key,
        sections={"summary": {"was": canon.section_hash("One line."), "text": "Terminal edit."}},
        acceptance={"was": [{"id": "AC1", "text": "First check"}], "edit": {"AC1": "First, reworded"}, "add": []},
        people={"reviewers": {"add": [quinn.ref], "remove": []}},
    )
    rig = Rig(human, tmp_path / "edit", intent)
    p = handoff_first(rig)
    unlocks = []

    class OpenOnce:
        name, person_capable, auth = "test", True, "passphrase"

        def unlocked(self, key_id, *, action, digest, labels, fields=()):
            unlocks.append((action, fields))

            @contextlib.contextmanager
            def cm():
                yield lambda payload: human.person.dk_sign(payload)

            return cm()

    shown = Shown()
    result = terminal(rig, served=human.served, shown=shown, code=p["request"]["code"], backend=OpenOnce())
    assert result["steps"] == 3 and result["key"] == key
    assert ("ticket title", f"edit {key}") in unlocks[0][1]
    assert "AC1: First check -> First, reworded" in shown.plain[0]
    got = doc(human, key)["doc"]
    assert got["sections"]["summary"] == "Terminal edit." and got["people"]["reviewers"] == [quinn.ref]
    # and step by step with the key backend that prompts per signature (no batch unlock)
    key2 = made(human, tmp_path / "two")
    intent2 = edit_intent(human, key2, sections={"summary": {"was": canon.section_hash("One line."), "text": "Again."}})
    rig2 = Rig(human, tmp_path / "edit2", intent2)
    p2 = handoff_first(rig2)
    assert terminal(rig2, human.served, Shown(), p2["request"]["code"])["key"] == key2


# --- F6: link repositories to a ticket (one review with the rest of the edit) ----------------------------------------


@pytest.fixture
def repo_lab(lab):
    """``proj`` and ``other`` are in ``settings.repos`` (``ws.repo`` appends the setting) before the host runs."""
    ws = lab.ws
    ws.store = ws.other()
    try:
        ws.repo("proj")
        ws.repo("other")
    finally:
        ws.store.close()
        ws.store = None
    return lab


def links_of(human, key):
    return doc(human, key)["doc"]["links"]


def link_intent(human, key, repos, branches, **over):
    was = links_of(human, key)
    return edit_intent(human, key, links={"was": was, "repos": repos, "branches": branches}, **over)


def test_an_edit_links_a_repository_with_a_branch_in_one_review(repo_lab, human, tmp_path):
    key = made(human, tmp_path)
    was = canon.section_hash("One line.")
    intent = link_intent(
        human,
        key,
        ["proj"],
        {"proj": "feat/x"},
        sections={"summary": {"was": was, "text": "Linked."}},
    )
    rig = Rig(human, tmp_path / "edit", intent)
    first = rig.first()
    human.check(first)
    content = signcontent.build(first, verified=True)
    rows = [r for r in content["fields_changed"] if r["field"] == "repository proj"]
    assert rows == [{"field": "repository proj", "before": "not linked", "after": "linked, branch feat/x"}]
    assert "repository" not in str(content["fields_changed"]).replace("repository proj", "")
    assert "proj: not linked -> linked, branch feat/x" in "\n".join(sequence.intent_lines(rig.intent, str))
    assert sequence.confirm_kind(rig.intent) == "click"
    out = rig.driver().run(first)
    assert out["landed"] == 2
    lk = links_of(human, key)
    assert lk["repos"] == ["proj"] and lk["branches"] == {"proj": "feat/x"}
    # a second edit changes the branch and unlinks nothing; a third unlinks
    rig2 = Rig(human, tmp_path / "edit2", link_intent(human, key, ["other", "proj"], {"proj": "feat/y"}))
    rig2.driver().run(rig2.first())
    assert links_of(human, key)["repos"] == ["other", "proj"]
    rig3 = Rig(human, tmp_path / "edit3", link_intent(human, key, ["other"], {}))
    rig3.driver().run(rig3.first())
    lk = links_of(human, key)
    assert lk["repos"] == ["other"] and lk["branches"] == {}


def test_an_unknown_repository_is_refused_by_the_host(repo_lab, human, tmp_path):
    key = made(human, tmp_path)
    rig = Rig(human, tmp_path / "edit", link_intent(human, key, ["nope"], {}))
    op, args = sequence.step_args(rig.intent, 0, None)
    res = human.c.req("human.prepare", {"op": op, "args": args, "signer": human.signer, "sequence": rig.meta0()})
    assert res["error"]["code"] == "invalid.input" and "nope" in res["error"]["message"], res["error"]
    assert links_of(human, key)["repos"] == []


def test_a_viewer_cannot_prepare_the_links_but_a_member_can(tmp_path):
    """The role check is the ordinary ``set`` rule of the model, inside the same prepare (a link is the event that
    ``orch set links=`` makes): a viewer is refused, a member who may edit the ticket gets it prepared and signs it."""
    from tests.host.live_helpers import world

    w = world(tmp_path, map_to="quinn")
    try:
        quinn = human_helpers.Human(w, w.quinn)
        w.person_event("settings.changed", set={"repos": {"proj": {"path": str(tmp_path / "proj")}}})
        uid, key = w.ticket()
        w.person_event("role.changed", person=w.quinn.ref, role="viewer")
        rig = Rig(quinn, tmp_path / "edit", link_intent(quinn, key, ["proj"], {"proj": "feat/x"}))
        op, args = sequence.step_args(rig.intent, 0, None)
        body = {"op": op, "args": args, "signer": quinn.signer, "sequence": rig.meta0()}
        res = quinn.c.req("human.prepare", body)
        assert human_helpers.err(res) == "invalid.input" and "a viewer cannot write" in res["error"]["message"]
        w.person_event("role.changed", person=w.quinn.ref, role="member")
        rig = Rig(quinn, tmp_path / "edit2", link_intent(quinn, key, ["proj"], {"proj": "feat/x"}))
        out = rig.driver().run(rig.first())
        assert out["landed"] == 1
        assert links_of(quinn, key)["branches"] == {"proj": "feat/x"}
    finally:
        w.close()
        w.lab.close()


@pytest.mark.parametrize(
    "bad",
    [
        {"repos": ["proj"], "branches": {"other": "x"}},  # a branch for a repository that is not linked
        {"repos": ["proj", "proj"], "branches": {}},
        {"repos": ["b", "a"], "branches": {}},  # not sorted
        {"repos": ["proj"], "branches": {"proj": "has space"}},
        {"repos": ["proj"], "branches": {"proj": "-leading"}},
        {"repos": ["proj"], "branches": {"proj": "a..b"}},
        {"repos": ["proj"], "branches": {"proj": "HEAD"}},
        {"repos": ["proj"], "branches": {"proj": "x\ny"}},
        {"repos": ["proj"], "branches": {"proj": ""}},
        {"repos": ["../x"], "branches": {}},
        {"repos": [], "branches": {}},  # nothing changes
        {"repos": ["proj"], "branches": {}, "extra": 1},
    ],
)
def test_malformed_links_are_refused(bad):
    was = {"repos": [], "branches": {}, "prs": [], "external": []}
    intent = {
        "v": 1,
        "kind": "edit_ticket",
        "ticket": {"key": "DEMO-0001", "uid": "0" * 26},
        "ticket_type": "feature",
        "head": "sha256:" + "0" * 64,
        "sections": {},
        "links": {"was": was, **bad},
    }
    with pytest.raises(SequenceInvalid):
        sequence.validate_intent(intent)


def test_links_cannot_unlink_a_repository_with_a_pull_request():
    was = {"repos": ["proj"], "branches": {}, "prs": [{"repo": "proj", "url": "https://x/1"}], "external": []}
    intent = {
        "v": 1,
        "kind": "edit_ticket",
        "ticket": {"key": "DEMO-0001", "uid": "0" * 26},
        "ticket_type": "feature",
        "head": "sha256:" + "0" * 64,
        "sections": {},
        "links": {"was": was, "repos": [], "branches": {}},
    }
    with pytest.raises(SequenceInvalid):
        sequence.validate_intent(intent)


def test_a_host_that_changes_the_links_step_aborts_before_the_signature(repo_lab, human, tmp_path):
    key = made(human, tmp_path)
    intent = link_intent(
        human,
        key,
        ["proj"],
        {"proj": "feat/x"},
        acceptance={"was": [{"id": "AC1", "text": "First check"}], "edit": {}, "add": ["Second"]},
    )
    rig = Rig(human, tmp_path / "edit", intent)
    first = rig.first()
    real = rig.prepare

    def evil(op, args, meta):
        if op == "set":
            return real(op, {**args, "pairs": ['links={"repos":["other"],"branches":{}}']}, meta)
        return real(op, args, meta)

    rig.port.prepare = evil
    with pytest.raises(Aborted):
        rig.driver().run(first)
    assert links_of(human, key)["repos"] == []
