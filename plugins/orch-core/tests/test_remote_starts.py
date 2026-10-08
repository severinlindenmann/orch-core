"""A new terminal session and an agent start from a paired device: never on the typing lease, each its own fresh
assertion over a subject that says exactly what starts. The real bridge host (route hook, assertion check, replay
record), the real dashboard app behind it; tmux and the launcher are fakes."""
import json
import re

import pytest

pytest.importorskip("cryptography")
pytest.importorskip("fastapi")

import test_bridge_host as H  # noqa: E402
from orch.core import quick, store  # noqa: E402
from orch.dashboard import launch, remote_gate, terminals  # noqa: E402
from orch.dashboard.bridge_loop import LEASE_ROUTES, START_ROUTES  # noqa: E402
from orch.dashboard.reach import Scope  # noqa: E402
from orch.remote.bridge_host import envelope as E  # noqa: E402
from test_bridge_host import KEY_A, KEY_B, NEW  # noqa: E402
from test_factory_bridge import FORM, bridge  # noqa: E402,F401
from test_factory_dashboard import _refine, fa, fh, fws  # noqa: E402,F401
from test_remote_terminals import tmux  # noqa: E402,F401


@pytest.fixture
def started(monkeypatch):
    """The launches the routes made, as (name, argv): launch.start is a recorder, the preflight a no-op."""
    calls = []
    monkeypatch.setattr(launch, "start", lambda ws, key, argv, **kw: calls.append((kw["name"], list(argv))) or "Opened")
    monkeypatch.setattr(launch, "preflight", lambda *a, **k: None)
    monkeypatch.setattr(launch, "load_settings", lambda: {**launch._defaults(), "factory_command": None})  # noqa: SLF001
    return calls


@pytest.fixture
def ticket(fws, fa, fh):
    t = fa.new("Fix the <login> page")
    _refine(fa, t.id)
    fh.approve(t.id, "requirements")
    return t.id


@pytest.fixture
def qid(fws, monkeypatch):
    task = {"id": "Q-1", "title": "Tidy the notes", "area": None, "status": "open", "added": {}, "claim": None,
            "done": None, "outgrew": None, "waived": False, "ticket": None, "artifacts": [], "notes": []}
    quick._save(fws, task)
    real = quick.settings
    monkeypatch.setattr(quick, "settings", lambda ws: {**real(ws), "enabled": True, "claim_minutes": 30})
    return task["id"]


def agent_body(mode="work", harness="claude", extra=""):
    return f"mode={mode}&harness={harness}&where=tmux{extra}"


def subject_of(bridge, path, body, key=KEY_A):
    v, _ = bridge.post(key, path, body)
    assert v.code == "assertion_required", (path, body, v.code, v.fields)
    return v.fields["subject"]


def starts(ticket, qid):
    """The three starts: (path, body)."""
    return [("/terminals/new", ""), (f"/t/{ticket}/agent/start", agent_body()), (f"/quick/{qid}/agent/start", "where=tmux")]


# -- the tags and the hook ------------------------------------------------------------------------------------------

def test_a_start_is_never_a_lease_route_and_the_gate_wants_it_fresh():
    assert not set(START_ROUTES) & {p for _, p in LEASE_ROUTES}
    for path in START_ROUTES:
        tag = remote_gate.TAGS[("POST", path)]
        assert tag.scope is Scope.TYPE and tag.fresh, path
    # derived, not listed: every POST route that starts a session or an agent is a start route
    for method, path in remote_gate.TAGS:
        if method == "POST" and (path.endswith("/agent/start") or path == "/terminals/new"):
            assert path in START_ROUTES and (method, path) not in LEASE_ROUTES, path
    assert LEASE_ROUTES == {("POST", "/terminals/{name}/keys"), ("POST", "/terminals/{name}/size"),
                            ("POST", "/terminals/{name}/end")}


def test_the_gate_refuses_a_start_without_a_fresh_assertion_whatever_the_scope(bridge, ticket, qid, tmux, started):
    for path, body in starts(ticket, qid):
        assert bridge.raw(Scope.TYPE, False, "POST", path, body)[0] == 403, path
    assert started == []


# -- what the person is shown ---------------------------------------------------------------------------------------

def test_each_start_states_exactly_what_it_starts(bridge, ticket, qid, tmux, started, fws):
    s = subject_of(bridge, "/terminals/new", "")
    assert s["kind"] == "action" and re.fullmatch("[0-9a-f]{64}", s["digest"])
    assert s["shown"].startswith("Start a new terminal session scratch running claude with no prompt, in ")
    assert str(fws.root) in s["shown"] and "Command: " in s["shown"]
    s = subject_of(bridge, f"/t/{ticket}/agent/start", agent_body())
    assert s["shown"] == (f"Start an agent. Harness claude, mode work, in Mission Control terminals as session {ticket}. "
                          f'Ticket {ticket} titled: "Fix the <login> page"')
    assert "alongside" not in s["shown"]
    assert "alongside the one already running" in subject_of(
        bridge, f"/t/{ticket}/agent/start", agent_body(extra="&another=1"))["shown"]
    s = subject_of(bridge, f"/quick/{qid}/agent/start", "where=tmux")
    assert s["shown"].startswith("Start an agent. Harness claude, in Mission Control terminals as session Q-1. ")
    assert s["shown"].endswith('Quick task Q-1 titled: "Tidy the notes"')
    assert started == []  # a challenge starts nothing


@pytest.mark.parametrize("change", [
    "mode=refine&harness=claude&where=tmux", "mode=work&harness=codex&where=tmux", "mode=work&harness=claude&where=",
    "mode=work&harness=claude&where=tmux&another=1", "mode=work&harness=claude&where=tmux&next=%2Fboard"])
def test_every_field_of_an_agent_start_changes_the_subject(bridge, ticket, tmux, started, fa, change):
    base = subject_of(bridge, f"/t/{ticket}/agent/start", agent_body())
    other = subject_of(bridge, f"/t/{ticket}/agent/start", change)
    assert other["digest"] != base["digest"]
    t2 = fa.new("another")
    assert subject_of(bridge, f"/t/{t2.id}/agent/start", agent_body())["digest"] != base["digest"]


def test_a_where_the_route_ignores_still_changes_the_subject(bridge, ticket, tmux, started):
    a = subject_of(bridge, f"/t/{ticket}/agent/start", "mode=work&harness=claude&where=x")
    b = subject_of(bridge, f"/t/{ticket}/agent/start", "mode=work&harness=claude&where=y")
    assert a["digest"] != b["digest"]  # same sheet text, so the digest alone must tell the two bodies apart


def test_a_changed_command_changes_the_subject_of_a_new_session(bridge, tmux, started, monkeypatch):
    from orch.dashboard.data import agent_start
    base = subject_of(bridge, "/terminals/new", "")
    real = agent_start.harnesses
    monkeypatch.setattr(agent_start, "harnesses", lambda ws, settings: {**real(ws, settings), "claude": ["claude", "--other"]})
    other = subject_of(bridge, "/terminals/new", "")
    assert other["digest"] != base["digest"] and "--other" in other["shown"]


def test_every_field_of_a_quick_start_changes_the_subject(bridge, qid, tmux, started):
    base = subject_of(bridge, f"/quick/{qid}/agent/start", "where=tmux")
    for body in ("where=", "where=tmux&next=%2Fquick"):
        assert subject_of(bridge, f"/quick/{qid}/agent/start", body)["digest"] != base["digest"], body


def test_the_same_request_gets_the_same_subject_and_the_session_name_is_bound(bridge, ticket, tmux, started, monkeypatch):
    first = subject_of(bridge, "/terminals/new", "")
    assert subject_of(bridge, "/terminals/new", "") == first
    monkeypatch.setattr(terminals, "free_name", lambda ws, base: base + "-9")
    taken = subject_of(bridge, "/terminals/new", "")
    assert taken["digest"] != first["digest"] and "session scratch-9 " in taken["shown"]


# -- a request the route would read differently gets no challenge -----------------------------------------------------

def test_an_ambiguous_or_unreadable_request_gets_no_challenge(bridge, ticket, qid, tmux, started):
    cases = [(f"/t/{ticket}/agent/start", agent_body() + "&mode=refine"),  # a field twice
             (f"/t/{ticket}/agent/start", agent_body() + "&harness=codex"),
             (f"/t/{ticket}/agent/start?mode=work&harness=claude", "where=tmux"),  # the query is never read
             (f"/t/{ticket}/agent/start", agent_body() + "&next=%C3%A9\xe9"),  # not ASCII
             (f"/t/{ticket}/agent/start", agent_body() + ";mode=refine"),  # a ";" splits some parsers
             (f"/t/{ticket}/agent/start", "harness=claude&where=tmux"),  # a field the route needs is absent
             (f"/quick/{qid}/agent/start", "where=tmux&where="),
             ("/terminals/new", "x=1"),  # the route reads nothing: no body is acceptable
             ("/terminals/new?x=1", "x=1")]
    for path, body in cases:
        e = bridge._env(KEY_A, {"op": "http", "method": "POST", "path": path, "headers": FORM},
                        body.encode("latin-1"))
        v = bridge._decide(e)
        assert v.code == "assertion_failed" and v.why == "no_subject", (path, body, v.code, v.why)
    assert started == []


def test_a_body_that_is_not_form_encoded_gets_no_challenge(bridge, ticket, tmux, started):
    e = bridge._env(KEY_A, {"op": "http", "method": "POST", "path": f"/t/{ticket}/agent/start",
                            "headers": {"content-type": "application/json"}}, json.dumps(
                                {"mode": "work", "harness": "claude"}).encode())
    assert bridge._decide(e).code == "assertion_failed"
    assert started == []


def test_the_query_is_never_read_for_a_new_session(bridge, tmux, started):
    assert subject_of(bridge, "/terminals/new?x=1", "") == subject_of(bridge, "/terminals/new", "")


# -- who may start ---------------------------------------------------------------------------------------------------

def test_a_decide_or_operate_device_cannot_start_even_asking(bridge, ticket, qid, tmux, started):
    for key in (KEY_B, NEW):
        for path, body in starts(ticket, qid):
            v, _ = bridge.post(key, path, body)
            assert v.code == "forbidden_scope", (key, path)
    assert started == []


# -- the whole round, once ---------------------------------------------------------------------------------------------

def test_a_start_runs_once_after_its_assertion_and_not_again(bridge, ticket, qid, tmux, started):
    for n, (path, body) in enumerate(starts(ticket, qid), 1):
        v, run, status, loc = bridge.fresh(KEY_A, path, body)
        assert v.code == "assertion_required" and run.result == "run" and status == 303, (path, status)
        assert "err=" not in loc, loc
        assert len(started) == n, path
    assert [c[0] for c in started] == ["scratch", ticket, "Q-1"]
    assert started[0][1] and "{prompt}" not in " ".join(started[0][1])
    shown = subject_of(bridge, "/terminals/new", "")["shown"]  # the session is taken now, but the command is the same
    assert shown.endswith("Command: " + " ".join(started[0][1]))


def test_a_replayed_assertion_or_request_starts_nothing_more(bridge, ticket, tmux, started):
    path, body = f"/t/{ticket}/agent/start", agent_body()
    v, r1 = bridge.post(KEY_A, path, body)
    run, a1 = bridge.answer(KEY_A, v, r1)
    assert run.result == "run" and bridge.run(run)[0] == 303 and len(started) == 1
    again = bridge._decide(a1)  # the very same assertion envelope
    assert again.result != "run"
    again = bridge._decide(r1)  # the very same request envelope
    assert again.result != "run"
    assert len(started) == 1


def test_an_assertion_over_another_request_or_subject_starts_nothing(bridge, ticket, tmux, started, fa):
    path = f"/t/{ticket}/agent/start"
    v1, r1 = bridge.post(KEY_A, path, agent_body())
    other = subject_of(bridge, path, agent_body("refine"))
    bad, _ = bridge.answer(KEY_A, v1, r1, subject=other)  # signed over what was not shown
    assert bad.code == "assertion_failed" and started == []
    v2, r2 = bridge.post(KEY_A, path, agent_body("refine"))
    bad, _ = bridge.answer(KEY_A, v2, r1)  # this challenge, the other parked request
    assert bad.code == "assertion_failed" and started == []


def test_an_agent_whose_session_name_was_taken_meanwhile_starts_nothing(bridge, ticket, tmux, started, monkeypatch):
    v, r1 = bridge.post(KEY_A, f"/t/{ticket}/agent/start", agent_body())
    monkeypatch.setattr(terminals, "free_name", lambda ws, base: base + "-2")
    run, _ = bridge.answer(KEY_A, v, r1)
    assert run.result != "run" and started == []


def test_a_new_session_whose_name_was_taken_meanwhile_starts_nothing(bridge, tmux, started, monkeypatch):
    v, r1 = bridge.post(KEY_A, "/terminals/new", "")
    assert "session scratch " in v.fields["subject"]["shown"]
    monkeypatch.setattr(terminals, "free_name", lambda ws, base: "scratch-2")
    run, _ = bridge.answer(KEY_A, v, r1)
    assert run.result != "run" and started == []


# -- the lease never reaches a start ---------------------------------------------------------------------------------

def _lease(bridge, tmux):
    """A Type device opens a terminal stream and unlocks the typing lease; returns the stream id."""
    stream_env = H.env(KEY_A, {"op": "http", "method": "GET", "path": "/terminals/DEMO-1/stream"},
                       seq=bridge.seq.setdefault(H.did(KEY_A), 0) + 1, ts=bridge.clock.now, flags=E.F_STREAM)
    bridge.seq[H.did(KEY_A)] += 1
    acc = bridge.host.check(stream_env, H.rid_of(stream_env))
    assert bridge.host.authorize(acc).result == "run"
    stream = bytes.fromhex(H.rid_of(stream_env))
    keys = H.env(KEY_A, {"op": "http", "method": "POST", "path": "/terminals/DEMO-1/keys", "headers": FORM},
                 b'{"seq":[],"n":1}', seq=bridge.seq[H.did(KEY_A)] + 1, ts=bridge.clock.now, stream=stream)
    bridge.seq[H.did(KEY_A)] += 1
    refusal = bridge._decide(keys)
    assert refusal.code == "lease_required"
    return stream, refusal, keys


def test_the_lease_sheet_names_the_terminal_and_says_it_covers_all(bridge, tmux):
    _, refusal, _ = _lease(bridge, tmux)
    shown = refusal.fields["subject"]["shown"]
    assert shown.startswith("Type into terminal DEMO-1 for 15 minutes.")
    assert "every terminal on this computer for 15 minutes" in shown and refusal.fields["subject"]["kind"] == "lease"


def test_a_lease_does_not_authorise_a_start(bridge, ticket, qid, tmux, started):
    stream, refusal, keys = _lease(bridge, tmux)
    run, _ = bridge.answer(KEY_A, refusal, keys)
    assert run.result == "run"  # the lease is open: typing runs
    for path, body in starts(ticket, qid):
        seq = bridge.seq[H.did(KEY_A)] = bridge.seq[H.did(KEY_A)] + 1
        e = H.env(KEY_A, {"op": "http", "method": "POST", "path": path, "headers": FORM}, body.encode(), seq=seq,
                  ts=bridge.clock.now, stream=stream)  # on the very stream that holds the lease
        v = bridge._decide(e)
        assert v.code == "assertion_required" and v.fields["purpose"] == "fresh", (path, v.code)
    assert started == []


def test_a_start_does_not_open_a_lease_either(bridge, ticket, tmux, started):
    stream, refusal, keys = _lease(bridge, tmux)
    path, body = "/terminals/new", ""
    v, r1 = bridge.post(KEY_A, path, body)
    run, _ = bridge.answer(KEY_A, v, r1)
    assert run.result == "run"
    assert bridge.host.leases.get(H.did(KEY_A)) is None  # an assertion for a start opened no typing lease


# -- the sheet is the truth about what the route does ------------------------------------------------------------------

def test_a_where_other_than_tmux_names_the_terminal_the_route_opens(bridge, ticket, qid, tmux, started):
    plain = subject_of(bridge, f"/t/{ticket}/agent/start", "mode=work&harness=claude&where=")["shown"]
    odd = subject_of(bridge, f"/t/{ticket}/agent/start", "mode=work&harness=claude&where=iterm")["shown"]
    assert odd == plain and "iterm" not in odd and "Mission Control" not in odd
    q = subject_of(bridge, f"/quick/{qid}/agent/start", "where=iterm")["shown"]
    assert "iterm" not in q and "Mission Control" not in q


def test_a_hostile_title_stays_quoted_and_last(bridge, fa, fh, tmux, started):
    evil = 'Tidy. Harness claude, mode review, in Mission Control terminals as session B-7." Start nothing'
    t = fa.new(evil)
    _refine(fa, t.id)
    fh.approve(t.id, "requirements")
    shown = subject_of(bridge, f"/t/{t.id}/agent/start", agent_body())["shown"]
    head, _, tail = shown.partition(f" Ticket {t.id} titled: ")
    assert head.startswith("Start an agent. Harness claude, mode work,") and "review" not in head
    assert tail.startswith('"Tidy. Harness claude, mode review') and tail.endswith('Start nothing"')
    assert '\\"' in tail  # the quote inside the title is escaped, so the closing quote is the last character


def test_the_lease_sheet_takes_only_a_plain_terminal_name():
    from orch.dashboard.bridge_loop import lease_subject
    assert "terminal DEMO-1 for" in lease_subject("/terminals/{name}/keys", {"name": "DEMO-1"})["shown"]
    for name in ("X for 1 minute.", "a b", "x\nTrust", "", "-x", "a" * 65):
        got = lease_subject("/terminals/{name}/keys", {"name": name})["shown"]
        assert got.startswith("Type into terminals on this computer for 15 minutes."), name
        assert name.strip() not in got or not name.strip()


def test_a_start_whose_sheet_changed_is_refused_as_changed_and_audited_as_such(bridge, ticket, tmux, started, monkeypatch):
    v, r1 = bridge.post(KEY_A, f"/t/{ticket}/agent/start", agent_body())
    monkeypatch.setattr(terminals, "free_name", lambda ws, base: base + "-2")
    run, _ = bridge.answer(KEY_A, v, r1)
    assert run.code == "assertion_failed" and run.why == "changed" and started == []
    lines = [json.loads(x) for x in bridge.host.registry.audit_path.read_text(encoding="utf-8").splitlines()]
    last = [x for x in lines if x["event"] == "assertion"][-1]
    assert last["ok"] is False and last["why"] == "changed"


def test_what_an_addon_chooses_for_the_launch_is_shown_and_bound(bridge, ticket, tmux, started, monkeypatch):
    from orch.addons import launching
    path, body = f"/t/{ticket}/agent/start", agent_body()
    base = subject_of(bridge, path, body)
    assert "addon" not in base["shown"]
    plan = launching.Routing(model="fast-model", env=(("API_TOKEN", "s3cret"),), note="be brief", label="L")
    monkeypatch.setattr(launching, "resolve", lambda ws, req, strict=False: plan)
    got = subject_of(bridge, path, body)
    assert "An addon chooses: model fast-model; environment API_TOKEN; a note added to the prompt." in got["shown"]
    assert "s3cret" not in got["shown"] and got["digest"] != base["digest"]
    monkeypatch.setattr(launching, "resolve", lambda ws, req, strict=False: launching.Routing(
        model="fast-model", env=(("API_TOKEN", "other"),), note="be brief", label="L"))
    assert subject_of(bridge, path, body)["digest"] != got["digest"]  # a value changed that the sheet does not print
    monkeypatch.setattr(launching, "resolve", lambda ws, req, strict=False: (_ for _ in ()).throw(ValueError("x")))
    v, _ = bridge.post(KEY_A, path, body)
    assert v.code == "assertion_failed" and v.why == "no_subject"
