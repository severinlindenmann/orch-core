"""``orch answer``: a person answers an agent's question; the question hash comes from the log."""

from __future__ import annotations

from tests.ops.helpers import wait_for
from tests.ops.humans import make_ticket


def ask(agent, text="Which export?", *extra):
    r = agent("ask", text, "--options", "csv,api", "--rec", "csv", *extra)
    assert r.code == 0, r.err
    return r


def answered(ws):
    return [e for e in ws.events("1") if e["type"] == "question.answered"]


def test_an_option_answer_is_signed_and_reaches_the_waiting_agent(hws, agent, me):
    key = make_ticket(agent)
    ask(agent)
    r = me("answer", "Q1", "--option", "api", "--ref", key)
    assert r.code == 0, r.err
    assert r.first.startswith(f"ok {key} question.answered Q1 seq=")
    (e,) = answered(hws)
    q = hws.view("1").questions[0]
    assert e["question"] == "Q1" and e["option"] == "api" and e["hash"] == q.hash and "text" not in e
    assert e["actor"]["id"] == hws.owner.ref and e["auth"] == "passphrase" and "sig" in e
    d = wait_for(agent, "answered")
    assert d.data["question"] == "Q1" and d.data["option"] == "api" and d.data["by"] == hws.owner.ref


def test_text_alone_or_with_an_option(hws, agent, me):
    key = make_ticket(agent)
    assert agent("ask", "Which export?").code == 0  # no options: a text question takes a text alone (#1123)
    ask(agent, "And the schema?")
    assert me("answer", "Q1", "-m", "use the API, it is complete", "--ref", key).code == 0
    assert me("answer", "Q2", "--option", "csv", "-m", "as discussed", "--ref", key).code == 0
    a, b = answered(hws)
    assert a["text"] == "use the API, it is complete" and "option" not in a
    assert b["option"] == "csv" and b["text"] == "as discussed"


def test_refusals_write_nothing(hws, agent, me):
    key = make_ticket(agent)
    ask(agent)
    n = len(hws.events("1"))
    hws.provider.requests.clear()
    assert me("answer", "Q1", "--ref", key, "--json").err_code == "invalid.input"  # neither option nor text
    assert me("answer", "Q1", "--option", "nope", "--ref", key, "--json").err_code == "invalid.input"
    assert me("answer", "Q7", "--option", "csv", "--ref", key, "--json").err_code == "not_found"
    assert me("answer", "Q1", "--option", "csv", "--json").err_code == "ambiguous_ref"
    assert hws.provider.requests == [] and len(hws.events("1")) == n
    assert me("answer", "Q1", "--option", "csv", "--ref", key).code == 0
    r = me("answer", "Q1", "--option", "api", "--ref", key, "--json")
    assert r.code != 0 and len(answered(hws)) == 1  # the first valid answer won


def test_only_the_addressee_an_owner_or_a_maintainer_may_answer(hws, agent, me):
    key = make_ticket(agent)
    ask(agent)
    hws.act_as(hws.add_member("mia", "member"))
    r = me("answer", "Q1", "--option", "csv", "--ref", key, "--json")
    assert r.code == 3 and r.err_code == "role.denied", r.out
    assert answered(hws) == []


def test_a_text_with_a_hidden_control_character_is_refused(hws, agent, me):
    key = make_ticket(agent)
    ask(agent)
    r = me("answer", "Q1", "-m", "fine‮text", "--ref", key, "--json")
    assert r.code == 6 and r.err_code == "parse.text"


def test_answering_a_question_with_approve_never_approves_a_gate(hws, agent, me):
    """R3-C4d: an agent once asked "please review the requirements gate (approve / changes)". Answering that is only
    an answer: the gate stays open until the person signs ``orch approve``."""
    key = make_ticket(agent)
    r = agent(
        "ask",
        "Please review the requirements gate (orch approve or orch request-changes)",
        "--options",
        "approve,changes",
    )
    assert r.code == 0, r.err
    r = me("answer", "Q1", "--option", "approve", "--ref", key)
    assert r.code == 0, r.err
    assert [e["type"] for e in hws.events("1") if e["type"].startswith("gate.")] == []
    assert not hws.view("1").gates["requirements"].approved


# --- #1123 B2: the terminal review is the signed View's plain text ---------------------------------------------------


def test_the_terminal_review_is_the_signer_view_and_shows_every_bound_field(hws, agent, me):
    key = make_ticket(agent)
    ask(agent, "Which export?\nPick one.", "--why", "The seeds are stale.")
    assert me("answer", "Q1", "--option", "api", "-m", "as discussed", "--ref", key).code == 0
    review = hws.reviews[-1]
    q = hws.view("1").questions[0]
    for needle in (
        "Your choice", "| B · key api", "| title: api", "Your note; the choice is api", "| as discussed",
        "Question Q1", "| Which export?", "| Pick one.", "Why it was asked", "| The seeds are stale.",
        "recommended: A · key csv", "option csv: A · key csv", "option csv title: csv",
        "option api: B · key api", "option api title: api", f"question hash: {q.hash}",
    ):  # fmt: skip
        assert needle in review, f"{needle!r} not in the review:\n{review}"
    assert review.index("Your choice") < review.index("Your note") < review.index("Question")
    assert "your choice:" not in review  # the old label-only row is gone
    assert "(per host)" not in review.split("recommended:")[1].split("\n")[0]  # bound, never the host's word


def test_the_terminal_review_holds_every_field_of_the_frozen_questions():
    """The full #1123 shape (title, text, facts, cost, details, why, recommended, allow_other) from the frozen
    fixtures, through the same function ``orch answer`` shows the person."""
    import json
    from pathlib import Path
    from types import SimpleNamespace

    from orch.ops.commands import answer

    fixture = json.loads((Path(__file__).parents[1] / "vectors/f1/questions.json").read_text())
    forms = {f["name"]: f["raw_event"] for f in fixture["forms"]}
    view = SimpleNamespace(key="DEMO-0001", uid="01J9ZK4Q7M3R8T2V6X0B5N1C9D")
    h = SimpleNamespace(header=lambda v: [f"ticket: {v.key}", ""])
    cases = [("choice_full", {"option": "csv"}), ("choice_full", {"text": "own words"}),
             ("text_with_why_and_details", {"text": "Acme Bank"})]  # fmt: skip
    for form, answer_ in cases:
        raw = forms[form]
        q = raw["question"]
        event = {"type": "question.answered", "question": "Q1", "hash": raw["hash"], **answer_}
        out = "\n".join(answer._review(h, view, q, event))
        bound = [q["text"], q["why"], q["details"], raw["hash"], *answer_.values()]
        for o in q.get("options", []):
            bound += [
                o["key"],
                o["label"],
                o.get("text", ""),
                o.get("cost", ""),
                *(x for f in o.get("facts", []) for x in f),
            ]
        for value in bound:
            for line in value.replace("\t", "⟨U+0009⟩").split("\n"):
                assert line in out, f"{form}: {line!r} not in the terminal review"


def test_wait_labels_a_note_with_the_choice(hws, agent, me):
    key = make_ticket(agent)
    ask(agent)
    assert me("answer", "Q1", "--option", "csv", "-m", "as discussed", "--ref", key).code == 0
    r = agent("wait", "--ref", key, "--timeout", "1")
    assert r.code == 0 and "--- your note; the choice is csv [" in r.out and "as discussed" in r.out


def test_a_bad_option_gets_the_models_refusal_before_any_review(hws, agent, me):
    """L1 (Opus review of 224e4d19): the review is built only after the model's precheck, so a key that is not an
    option is refused as the model refuses it, and nothing is shown or signed."""
    key = make_ticket(agent)
    ask(agent)
    hws.reviews.clear()
    r = me("answer", "Q1", "--option", "zzz", "--ref", key, "--json")
    assert r.err_code == "invalid.input" and r.doc["error"]["message"].startswith("answer.bad_option:"), r.out
    assert hws.reviews == [] and answered(hws) == []
    d = me("answer", "Q1", "--option", "csv", "--ref", key, "--dry-run")  # a dry run shows no review either
    assert d.code == 0 and hws.reviews == [] and answered(hws) == []
