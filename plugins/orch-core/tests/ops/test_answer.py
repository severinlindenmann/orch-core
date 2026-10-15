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
    ask(agent)
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
