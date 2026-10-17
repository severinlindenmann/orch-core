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
    monkeypatch.setattr(sequence, "confirm_kind", lambda intent: "word")
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
