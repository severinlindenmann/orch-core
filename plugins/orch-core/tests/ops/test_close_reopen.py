"""``orch close`` and ``orch reopen``: ticket lifecycle decisions of the ticket owner, owners and maintainers."""

from __future__ import annotations

from tests.ops.humans import make_ticket, to_testing


def test_close_signs_a_resolution_and_the_ticket_is_closed(hws, agent, me):
    key = make_ticket(agent)
    r = me("close", key, "--resolution", "wont_do", "-m", "not worth it")
    assert r.code == 0, r.err
    e = [x for x in hws.events("1") if x["type"] == "ticket.closed"][0]
    assert e["resolution"] == "wont_do" and e["text"] == "not worth it" and e["auth"] == "passphrase"
    assert e["actor"]["id"] == hws.owner.ref and "sig" in e
    assert hws.view("1").status == "closed"


def test_close_defaults_to_other_and_duplicate_names_the_survivor(hws, agent, me):
    key = make_ticket(agent)
    other = agent("new", "Same thing", "-m", "dup").first.split()[1]
    assert (
        me("close", key, "--duplicate-of", other, "--json").err_code == "invalid.input"
    )  # needs --resolution duplicate
    r = me("close", key, "--resolution", "duplicate", "--duplicate-of", "2")
    assert r.code == 0, r.err
    e = [x for x in hws.events("1") if x["type"] == "ticket.closed"][0]
    assert e["resolution"] == "duplicate" and e["duplicate_of"] == other and "text" not in e
    r = me("close", other)
    assert r.code == 0 and [x for x in hws.events("2") if x["type"] == "ticket.closed"][0]["resolution"] == "other"


def test_reopen_raises_every_gate_and_needs_a_closed_or_done_ticket(hws, agent, me):
    key = make_ticket(agent)
    assert me("reopen", key, "--json").err_code == "transition.refused"  # it is open
    assert me("approve", "requirements", "--ref", key).code == 0
    gens = {g: v.gen for g, v in hws.view("1").gates.items()}
    assert me("close", key).code == 0
    r = me("reopen", key, "-m", "we need it after all")
    assert r.code == 0, r.err
    e = [x for x in hws.events("1") if x["type"] == "ticket.reopened"][0]
    assert e["text"] == "we need it after all" and e["auth"] == "passphrase"
    v = hws.view("1")
    assert (
        v.status != "closed"
        and all(v.gates[g].gen > gens[g] for g in gens if v.gates[g].applies)
        and not v.gates["requirements"].approved
    )


def test_a_done_ticket_is_reopened_by_a_person(hws, agent, me):
    key = make_ticket(agent)
    to_testing(agent, me, hws.tmp, key)
    assert me("verdict", "pass", "--ref", key).code == 0 and hws.view("1").status == "done"
    assert me("reopen", key, "-m", "regression").code == 0
    assert hws.view("1").status != "done"


def test_a_member_who_neither_owns_nor_maintains_may_not_close(hws, agent, me):
    key = make_ticket(agent)
    hws.act_as(hws.add_member("mia", "member"))
    hws.provider.requests.clear()
    r = me("close", key, "--json")
    assert r.code == 3 and r.err_code == "role.denied"
    assert hws.provider.requests == [] and hws.view("1").status != "closed"
