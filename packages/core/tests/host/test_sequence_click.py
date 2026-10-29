# ruff: noqa: F811
"""Batch 11, finding 9: the one-click path of the sequence. The checks run in the click signer's own process (the
dashboard server only relays), one click signs every step with the convenience key."""

from __future__ import annotations

import pytest

from orch.click.signer import ClickSigner  # noqa: F401
from tests.host import human_helpers
from tests.host.human_helpers import err, ok
from tests.host.test_click import Click, click, refused  # noqa: F401
from tests.host.test_sequence import intent_of

lab, served, human = human_helpers.lab, human_helpers.served, human_helpers.human

pytestmark = pytest.mark.slow


def seq_args(click_, intent=None):
    from orch.host import sequence

    intent = sequence.validate_intent(intent or intent_of())
    person = click_.owner.ref
    meta = {
        "step": 0,
        "of": len(sequence.steps(intent)),
        "digest": sequence.digest_of(click_.served.wid, person, intent),
        "intent": intent,
    }
    op, args = sequence.step_args(intent, 0, None)
    return intent, op, args, meta


def prepare(click_, intent=None):
    intent, op, args, meta = seq_args(click_, intent)
    return intent, click_.prepare(op, args, sequence=meta)


def test_one_click_shows_the_ticket_and_its_text_and_signs_every_step(click, served, human):
    _, p = prepare(click)
    v = click.view(p["request_id"])
    content = v["view"]["content"]
    assert sorted(s["id"] for s in content["sections"]) == ["context", "summary"]
    assert "Line two" in "\n".join(v["view"]["lines"])
    assert v["view"]["confirm"] == {"kind": "click"}
    click.mono.t += 2
    out = click.sign(p["request_id"], v["view_digest"])
    assert click.signatures == 4  # new, set, two sections: one click
    key = out["result"]["key"] if "key" in out["result"] else None
    got = ok(served.client().req("ticket.get", {"ref": key}))["data"]["doc"]
    assert got["title"] == "Add a sequence" and got["priority"] == "high" and got["labels"] == ["a", "b"]
    again = click.sign(p["request_id"], v["view_digest"])  # a repeated sign answers, never signs again
    assert again["duplicate"] is True and click.signatures == 4


def test_a_click_re_asserts_the_confirmation_kind_of_the_stored_intent(click, served, human, monkeypatch):
    from orch.host import sequence

    _, p = prepare(click)
    v = click.view(p["request_id"])
    click.mono.t += 2
    monkeypatch.setattr(sequence, "confirm_kind", lambda intent, person=None: "terminal")
    refused(lambda: click.sign(p["request_id"], v["view_digest"]), "human.click_refused", "terminal_only")
    assert click.signatures == 0


def test_a_foreign_event_in_between_ends_the_sequence_and_leaves_a_draft(click, served, human):
    _, p = prepare(click)
    v = click.view(p["request_id"])
    click.mono.t += 2
    state = {}

    def tamper(op, args, res):
        if op == "human.submit" and "n" not in state:  # after step 0 landed, somebody else writes
            state["n"] = 1
            key = res["result"]["key"] if "result" in res and "key" in res.get("result", {}) else res.get("key")
            state["key"] = key
            human.agent("log", {"ref": key, "message": "foreign"})
        return res

    click.tamper = tamper
    err_ = refused(lambda: click.sign(p["request_id"], v["view_digest"]), "human.click_refused", "stopped")
    assert "1 of 4 signatures landed" in err_.message and "DEMO-" in err_.message
    assert click.signatures == 1  # only the ticket itself was signed
    assert ok(served.client().req("ticket.get", {"ref": state["key"]}))["data"]["doc"]["title"] == "Add a sequence"


def test_a_lost_answer_is_resumable_but_not_by_a_click(click, served, human):
    _, p = prepare(click)
    v = click.view(p["request_id"])
    click.mono.t += 2
    calls = {"submits": 0}

    def tamper(op, args, res):
        if op == "human.submit":
            calls["submits"] += 1
            if calls["submits"] == 2:  # the host appended step 1 but the answer is lost
                raise ConnectionError("lost")
        return res

    click.tamper = tamper
    stopped = refused(lambda: click.sign(p["request_id"], v["view_digest"]), "human.click_refused", "resumable")
    assert "DEMO-" in stopped.message and "terminal" in stopped.message
    click.signer.cancel({"workspace_id": served.wid, "request_id": p["request_id"]})
    assert click.signatures == 2
    click.tamper = None
    # the same review again: a click never shows "create" again over a half-done ticket
    _, p2 = prepare(click)
    v2 = click.view(p2["request_id"])
    click.mono.t += 2
    refused(lambda: click.sign(p2["request_id"], v2["view_digest"]), "human.click_refused", "resumable")
    assert click.signatures == 2
    rows = ok(served.client().req("ticket.query", {}))["data"]
    rows = next(v for v in rows.values() if isinstance(v, list))
    assert len([t for t in rows if t["title"] == "Add a sequence"]) == 1


def test_a_sequence_is_new_and_nothing_else(click, served, human):
    intent, op, args, meta = seq_args(click)
    for bad_op, bad_args in (("log", {"ref": "DEMO-0001", "text": "x"}),):
        with pytest.raises(Exception, match="sequence"):
            click.prepare(bad_op, bad_args, sequence=meta)
    with pytest.raises(Exception, match="sequence"):
        click.prepare(op, args, sequence=meta, deposit=True)


def test_the_host_refuses_a_sequence_for_the_phone(human):
    from orch.host import sequence

    intent = sequence.validate_intent(intent_of())
    person = human.person.ref
    meta = {
        "step": 0,
        "of": len(sequence.steps(intent)),
        "digest": sequence.digest_of(human.served.wid, person, intent),
        "intent": intent,
    }
    op, args = sequence.step_args(intent, 0, None)
    res = human.c.req(
        "human.prepare",
        {"op": op, "args": args, "signer": {"device": human.person.device, "auth": "secure-enclave"}, "sequence": meta},
    )
    assert err(res) == "invalid.input"


def test_a_sequence_with_an_owner_step_is_one_click(click, served, human):
    """#1198 C1: an owner step decides who may act, and it is still one click after the review (no typed word)."""
    me = click.owner.ref  # another member would need his own reader declaration for convenience signing
    _, p = prepare(click, intent_of(acceptance=["It opens"], people={"reviewers": [me], "owner": me}))
    v = click.view(p["request_id"])
    assert v["view"]["confirm"] == {"kind": "click"}
    click.mono.t += 2
    out = click.sign(p["request_id"], v["view_digest"])
    assert click.signatures == 4 + 1 + 2  # new, set, two sections, the criterion, the reviewer, the owner
    got = ok(served.client().req("ticket.get", {"ref": out["result"]["key"]}))["data"]["doc"]
    assert [a["text"] for a in got["acceptance"]] == ["It opens"] and got["people"]["reviewers"] == [me]


def test_an_edit_sequence_is_signed_by_one_click_when_it_changes_no_people(click, served, human):
    from orch import canon

    made_p = prepare(click)[1]
    v = click.view(made_p["request_id"])
    click.mono.t += 2
    key = click.sign(made_p["request_id"], v["view_digest"])["result"]["key"]
    data = ok(served.client().req("ticket.get", {"ref": key}))["data"]
    intent = {
        "v": 1,
        "kind": "edit_ticket",
        "ticket": {"key": key, "uid": data["doc"]["uid"]},
        "ticket_type": "feature",
        "head": data["doc"]["head"]["head"],
        "sections": {"summary": {"was": canon.section_hash("One line."), "text": "A better line."}},
        "acceptance": {"was": [], "edit": {}, "add": ["It opens"]},
    }
    before = click.signatures
    _, p = prepare(click, intent)
    v = click.view(p["request_id"])
    assert v["view"]["confirm"] == {"kind": "click"}
    assert v["view"]["content"]["headline"].startswith(f"Edit {key} (key per host; signed id")
    assert v["view"]["content"]["ticket"]["new"] is False  # an edit: the signed id stays in the main view
    click.mono.t += 2
    click.sign(p["request_id"], v["view_digest"])
    assert click.signatures == before + 2
    got = ok(served.client().req("ticket.get", {"ref": key}))["data"]
    assert got["doc"]["sections"]["summary"] == "A better line." and got["doc"]["acceptance"][0]["text"] == "It opens"


def _title_edit(click, served, title_was, title_text, **extra):
    from orch import canon

    made_p = prepare(click)[1]
    v = click.view(made_p["request_id"])
    click.mono.t += 2
    key = click.sign(made_p["request_id"], v["view_digest"])["result"]["key"]
    data = ok(served.client().req("ticket.get", {"ref": key}))["data"]
    assert data["doc"]["title"] == "Add a sequence"
    intent = {
        "v": 1,
        "kind": "edit_ticket",
        "ticket": {"key": key, "uid": data["doc"]["uid"]},
        "ticket_type": "feature",
        "head": data["doc"]["head"]["head"],
        "sections": {"summary": {"was": canon.section_hash("One line."), "text": "A better line."}},
        "title": {"was": title_was, "text": title_text},
        **extra,
    }
    return key, intent


def test_r7_p07_an_edit_with_a_title_signs_a_set_title_step_and_the_review_shows_old_and_new(click, served, human):
    key, intent = _title_edit(click, served, "Add a sequence", "Add a better sequence")
    before = click.signatures
    _, p = prepare(click, intent)
    v = click.view(p["request_id"])
    rows = "\n".join(v["view"]["lines"])
    assert "Add a sequence" in rows and "Add a better sequence" in rows
    click.mono.t += 2
    click.sign(p["request_id"], v["view_digest"])
    assert click.signatures == before + 2  # the title, then the section
    got = ok(served.client().req("ticket.get", {"ref": key}))["data"]["doc"]
    assert got["title"] == "Add a better sequence" and got["sections"]["summary"] == "A better line."


def test_r7_p07_a_title_edit_alone_is_a_sequence_and_a_stale_old_title_is_refused(click, served, human):
    from orch.host import sequence

    key, intent = _title_edit(click, served, "Add a sequence", "Only the title")
    intent["sections"] = {}
    ok_intent = sequence.validate_intent(intent)
    assert [s["args"]["pairs"] for s in sequence.steps(ok_intent)] == [["title=Only the title"]]
    _, p = prepare(click, intent)
    v = click.view(p["request_id"])
    click.mono.t += 2
    click.sign(p["request_id"], v["view_digest"])
    assert ok(served.client().req("ticket.get", {"ref": key}))["data"]["doc"]["title"] == "Only the title"
    # a title edit made against a title the ticket no longer has (head is current, `was` is not): nothing is signed
    head = ok(served.client().req("ticket.get", {"ref": key}))["data"]["doc"]["head"]["head"]
    stale = {**intent, "head": head, "title": {"was": "Add a sequence", "text": "Another"}}
    signed = click.signatures
    with pytest.raises(Exception, match="ticket changed"):
        _, p = prepare(click, stale)
        v = click.view(p["request_id"])
        click.mono.t += 2
        click.sign(p["request_id"], v["view_digest"])
    assert click.signatures == signed
    assert ok(served.client().req("ticket.get", {"ref": key}))["data"]["doc"]["title"] == "Only the title"


@pytest.mark.parametrize("text", ["", "two\nlines", "x" * 201, "bell\x07", "Add a sequence", " lead", "trail ", "tab\there", "Fix\u200b", "a\u202eb"])
def test_r7_p07_the_title_is_one_line_1_to_200_and_different(text):
    from orch.host import sequence

    intent = {
        "v": 1,
        "kind": "edit_ticket",
        "ticket": {"key": "PW-0001", "uid": "01" + "A" * 24},
        "ticket_type": "feature",
        "head": "sha256:" + "0" * 64,
        "sections": {},
        "title": {"was": "Add a sequence", "text": text},
    }
    with pytest.raises(sequence.SequenceInvalid):
        sequence.validate_intent(intent)


def test_1070_a_new_ticket_with_a_reviewer_is_one_click_and_lists_the_person(click, served, human):
    """#1070: assignees/reviewers on a NEW ticket (the creator stays owner) sign by click, no typed word; the View
    still lists the person with role and full id."""
    me = click.owner.ref
    _, p = prepare(click, intent_of(people={"reviewers": [me]}))
    v = click.view(p["request_id"])
    assert v["view"]["confirm"] == {"kind": "click"}
    rows = v["view"]["content"]["fields_changed"]
    assert {"field": "reviewers: add", "before": None, "after": me} in [
        {"field": r["field"], "before": r.get("before"), "after": r["after"]} for r in rows
    ] or any(r["field"] == "reviewers: add" and r["after"] == me for r in rows)
    click.mono.t += 2
    out = click.sign(p["request_id"], v["view_digest"])
    got = ok(served.client().req("ticket.get", {"ref": out["result"]["key"]}))["data"]["doc"]
    assert got["people"]["reviewers"] == [me]


def test_1070_naming_another_member_or_a_mix_is_a_click_and_the_review_names_the_role(click, served, human):
    """#1198 C1: naming anyone else is a click too (no typed word); the review says what the role allows."""
    me = click.owner.ref
    other = "p_" + "9" * 32  # not a member
    for people in ({"reviewers": [other]}, {"assignees": [other]}, {"reviewers": sorted([me, other])}):
        _, p = prepare(click, intent_of(people=people))
        v = click.view(p["request_id"])
        assert v["view"]["confirm"] == {"kind": "click"}, people
        notes = str(v["view"]["content"].get("notes"))
        assert ("Reviewers may approve" in notes) or ("Assignees do the work" in notes)


def test_a_sequence_approval_not_classed_as_the_intent_is_refused_at_sign(click, served, human):
    """The class is re-asserted at sign time from the stored intent: an approval recorded as anything but the
    intent's class is refused and nothing is signed."""
    other = "p_" + "9" * 32
    _, p = prepare(click, intent_of(people={"reviewers": [other]}))
    v = click.view(p["request_id"])
    assert v["view"]["confirm"] == {"kind": "click"}
    approvals = [a for a in click.signer.approvals.values() if a.request_id == p["request_id"]]
    assert len(approvals) == 1
    approvals[0].kind = "terminal"  # as if the prepare had classed it differently
    v = click.view(p["request_id"])  # a view the page could now have been shown
    click.mono.t += 2
    refused(lambda: click.sign(p["request_id"], v["view_digest"]), "human.click_refused", "terminal_only")
    assert click.signatures == 0


def test_1070_self_as_assignee_and_reviewer_is_a_click_at_prepare_and_at_sign(click, served, human):
    me = click.owner.ref
    _, p = prepare(click, intent_of(people={"assignees": [me], "reviewers": [me]}))
    approvals = [a for a in click.signer.approvals.values() if a.request_id == p["request_id"]]
    assert approvals[0].kind == "click"  # classed at prepare
    v = click.view(p["request_id"])
    assert v["view"]["confirm"] == {"kind": "click"}
    click.mono.t += 2
    out = click.sign(p["request_id"], v["view_digest"])  # accepted at sign time
    got = ok(served.client().req("ticket.get", {"ref": out["result"]["key"]}))["data"]["doc"]
    assert got["people"]["assignees"] == [me] and got["people"]["reviewers"] == [me]


def test_c21_a_missing_key_refuses_a_sequence_before_any_step_is_claimed(click, served, human):
    _, p = prepare(click)
    v = click.view(p["request_id"])
    click.mono.t += 2
    gone = click.store.items.pop((served.wid, click.owner.device))
    refused(lambda: click.sign(p["request_id"], v["view_digest"]), "custody.no_key")
    assert click.signatures == 0 and (served.wid, p["request_id"]) not in click.signer._attempted
    click.store.items[(served.wid, click.owner.device)] = gone
    assert click.sign(p["request_id"], v["view_digest"])["duplicate"] is False
    assert click.signatures == 4
