"""claim, release and handoff (F1 5.2, 5.9, 10.3): one claim per ticket, takeover with a reason, subagents share it."""

from __future__ import annotations

from tests.ops.helpers import OTHER, SESSION


def make(cli, *titles, **kw):
    for t in titles:
        assert cli("new", t, *[x for k, v in kw.items() for x in (f"--{k}", v)]).code == 0


def test_claim_takes_the_ticket_and_moves_it_to_in_progress(ws, cli):
    make(cli, "A")
    r = cli("claim", "DEMO-0001")
    assert r.code == 0 and r.first == "ok DEMO-0001 claim.taken in_progress seq=2"
    assert "next: orch show" in r.out
    assert ws.view("DEMO-0001").claim.session == SESSION
    assert cli.j("claim", "1").code == 0  # your own live claim again: answered, nothing appended
    assert len(ws.read_events(ws.uid("1"))) == 2


def test_claim_next_takes_the_best_free_ticket(ws, cli):
    make(cli, "low one", priority="low")
    make(cli, "urgent one", priority="urgent")
    make(cli, "medium one")
    assert cli("claim", "--next").first.startswith("ok DEMO-0002 claim.taken")
    r = cli.j("claim")  # no REF: the next one, but this session already holds DEMO-0002
    assert (r.code, r.err_code) == (4, "claim.held") and "--also" in r.doc["error"]["message"]
    assert "orch claim REF --also" in r.doc["error"]["hint"]
    r = cli("claim", "--also")
    assert r.first.startswith("ok DEMO-0003 claim.taken")


def test_claim_next_skips_claimed_and_blocked_tickets(ws, cli):
    make(cli, "first", "second")
    assert cli("set", "2", "blocked_by=1").code == 0  # the creator knows what it made
    other = type(cli)(ws, session=OTHER)
    assert other("claim", "--next").first.startswith("ok DEMO-0001 claim.taken")
    r = cli.j("claim", "--next")  # 1 is claimed, 2 is blocked by 1
    assert r.code == 2 and r.err_code == "not_found"


def test_a_second_session_is_refused_and_can_take_over_with_a_reason(ws, cli):
    make(cli, "A")
    other = type(cli)(ws, session=OTHER)
    cli("claim", "1")
    r = other.j("claim", "1")
    assert (r.code, r.err_code) == (4, "claim.held")
    r = other.j("claim", "1", "--takeover")
    assert (r.code, r.err_code) == (5, "invalid.input") and "--reason" in r.doc["error"]["message"]
    r = other.j("claim", "1", "--takeover", "--reason", "the first one stalled")
    assert r.code == 0 and r.data == {"status": "in_progress", "takeover": True}
    v = ws.view("1")
    assert v.claim.session == OTHER and v.takeovers[-1]["from_session"] == SESSION
    assert cli.j("release", "1").err_code == "claim.required"  # the old session lost it


def test_takeover_of_an_unclaimed_ticket_and_reason_without_takeover_are_refused(ws, cli):
    make(cli, "A")
    assert cli.j("claim", "1", "--takeover", "--reason", "x").err_code == "invalid.input"
    assert cli.j("claim", "1", "--reason", "x").err_code == "invalid.input"


def test_release_lets_go_and_needs_the_claim(ws, cli):
    make(cli, "A")
    assert cli.j("release", "1").err_code == "claim.required"
    cli("claim", "1")
    r = cli("release")
    assert r.first == "ok DEMO-0001 claim.released released seq=3"
    assert ws.view("1").status == "open" and ws.view("1").claim is None
    assert cli.j("release", "1").err_code == "claim.required"
    assert cli("claim", "1").code == 0  # free again


def test_a_subagent_works_under_the_parents_claim_but_does_not_release_it(ws, cli):
    make(cli, "A")
    cli("claim", "1")
    sub = type(cli)(ws, session=SESSION + ".1")
    assert sub("status").code == 0 and "DEMO-0001" in sub("status").out
    assert sub("log", "note from the subagent").code == 0  # no REF: the parent's claim
    r = sub.j("release")  # the schema lets an agent release only the claim of its own session
    assert (r.code, r.err_code) == (4, "claim.required")
    assert cli("release").code == 0


def test_a_claim_lapses_after_claim_ttl_and_a_takeover_resumes_it(ws, cli):
    make(cli, "A")
    cli("claim", "1")
    ws.clock[0] += 3 * 3600  # past claim_ttl_min (120) and the 8 h grant is still valid
    r = cli.j("claim", "1")
    assert (r.code, r.err_code) == (4, "claim.held") and "lapsed" in r.doc["error"]["message"]
    r = cli.j("claim", "1", "--takeover", "--reason", "resume")
    assert r.code == 0


def test_handoff_writes_current_state_and_releases(ws, cli):
    make(cli, "A")
    cli("claim", "1")
    r = cli("handoff", "-m", "T1 done, T2 needs the API key")
    assert r.first == "ok DEMO-0001 handoff.written 29 seq=4", r.err
    v = ws.view("1")
    assert v.status == "open" and v.claim is None and v.handoff == "T1 done, T2 needs the API key"
    assert "T2 needs the API key" in cli("show", "1", "--section", "current_state").out
    assert [e["type"] for e in ws.read_events(v.uid)][-2:] == ["handoff.written", "claim.released"]


def test_handoff_needs_the_claim_one_source_and_at_most_2048_bytes(ws, cli):
    make(cli, "A")
    assert cli.j("handoff", "1", "-m", "x").err_code == "claim.required"
    cli("claim", "1")
    assert cli.j("handoff").err_code == "invalid.input"  # exactly one of -m / --file
    assert cli.j("handoff", "-m", "x", "--file", "-").err_code == "invalid.input"
    r = cli.j("handoff", "-m", "x" * 2049)
    assert (r.code, r.err_code) == (5, "invalid.input") and "2048" in r.doc["error"]["message"]
    assert ws.view("1").claim is not None  # nothing was written
    r = cli("handoff", "--file", "-", stdin="from stdin\r\nsecond line")
    assert r.code == 0 and ws.view("1").handoff == "from stdin\nsecond line"


def test_handoff_is_all_or_nothing(ws, cli):
    """The two events are judged together: a refusal of the second leaves the first unwritten."""
    make(cli, "A")
    cli("claim", "1")
    uid = ws.uid("1")
    n = len(ws.read_events(uid))
    r = cli.j("handoff", "-m", "bad \x07 text")
    assert r.code != 0 and len(ws.read_events(uid)) == n
