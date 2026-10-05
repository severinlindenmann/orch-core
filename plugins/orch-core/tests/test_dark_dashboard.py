"""Dark AI Factory on the dashboard (phase 5): the New ticket modes, the Dark start on the epic page, the run view, the
factory list and "Add to the Dark profile" on a card. Everything that starts or signs is the human's: a forged mode,
a missing typed word, Dark off, an agent harness, a missing cookie and a cross-origin post are refused."""
import json
import os
import re
from datetime import timedelta

import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient  # noqa: E402

from orch.core import dark_profile, epics, factory_report, factory_runner, factory_sessions as fs  # noqa: E402
from orch.core import ledger, permits, store  # noqa: E402
from orch.dashboard import launch  # noqa: E402

ASK = "Build the <b>export</b> & keep \"quotes\".\n\n### Detail\n- one line\n- `two` lines\n\nlast line"
DONE = "Exports open in the viewer."
CMD = "make <deploy> 'staging'"


class Fake:
    """list, start and stop runner sessions in memory (no tmux)"""
    def __init__(self):
        self.names, self.started = set(), []

    def alive(self):
        return set(self.names)

    def start(self, name, cwd, argv):
        self.names.add(name)
        self.started.append((name, cwd, argv))
        return 4242

    def stop(self, name):
        self.names.discard(name)


@pytest.fixture(autouse=True)
def _trusted_programs(monkeypatch):
    """No real programs or user settings are looked at (as in test_factory_runner)."""
    monkeypatch.setattr(factory_runner, "resolve_bin", lambda name: f"/opt/test/{os.path.basename(name)}")
    monkeypatch.setattr(factory_runner, "user_settings_blocker", lambda environ=None: None)


@pytest.fixture
def fws(configure):
    return configure(factory={"enabled": True})


@pytest.fixture
def dws(fws, human):
    from orch.core.ops import Ops
    Ops(fws, human).set_factory_dark(True)
    return fws


@pytest.fixture
def fa(fws, agent):
    from orch.core.ops import Ops
    return Ops(fws, agent)


@pytest.fixture
def fh(fws, human):
    from conftest import human_ops
    return human_ops(fws, human)


def _client(ws):
    from orch.dashboard.app import create_app
    c = TestClient(create_app(ws, "tok"))
    assert c.get("/?token=tok").status_code == 200
    return c


def _post(c, url, **data):
    return c.post(url, data=data, follow_redirects=False)


def _loc(resp):
    return resp.headers.get("location", "")


def _new(c, mode="dark", **over):
    data = {"title": "Export revamp", "mode": mode, "ask": ASK, "done_when": DONE, "size": "m", "priority": "normal"}
    if mode == "dark":
        data["confirm_dark"] = "dark"
    data.update(over)
    return _post(c, "/new", **data)


def _epic(ws, eid):
    return store.load(ws, eid)[1]


def _refine(ops, tid, plan="1. do it"):
    ops.set_section(tid, "Requirements", "r")
    ops.set_section(tid, "Acceptance criteria", "- [ ] a")
    if plan:
        ops.set_section(tid, "Plan", plan)


def _child(fa, eid, title="child"):
    c = fa.new(title, epic=eid)
    _refine(fa, c.id)
    fa.epic_auto_approve(c.id)
    return c.id


def _started(c, ws, mode="dark"):
    """An epic created and started from the New ticket page; its id."""
    r = _new(c, mode)
    assert "err=" not in _loc(r), _loc(r)
    return re.search(r"/factory/([A-Z]+-\d+)", _loc(r)).group(1)


def _tick(ws, human, fake):
    return factory_runner.tick(ws, human, fake, settings=launch.load_settings())


def _behavior(out):
    return out["hookSpecificOutput"]["decision"]["behavior"] if out else None


# -- the New ticket page: the mode choice --------------------------------------------------------------------------

def test_no_mode_choice_while_the_factory_is_off(dash):
    html = dash.get("/new").text
    assert 'name="mode"' not in html and "AI Factory" not in html and 'name="done_when"' not in html


def test_mode_choice_with_the_factory_on_and_dark_off(fws):
    html = _client(fws).get("/new").text
    assert 'id="mode-ticket"' in html and 'id="mode-factory"' in html and 'id="mode-dark"' not in html
    assert re.search(r'id="mode-ticket"[^>]*checked', html)
    assert "Dark AI Factory is off. Turn it on in a terminal with <code>orch factory dark on</code>." in html
    assert 'name="confirm_dark"' not in html
    assert "Everything in Requirements is built, checked and works as written." in html  # Done when, prefilled


def test_mode_choice_with_dark_on(dws):
    html = _client(dws).get("/new").text
    assert 'id="mode-dark"' in html and 'name="confirm_dark"' in html and "data-dark-off" not in html
    assert "You sign once, by typing dark." in html and "There is no release or automatic close yet" in html
    assert "A permission card appears when an agent needs a command" in html


# -- the New ticket page: creating and starting ----------------------------------------------------------------------

def test_dark_start_from_the_new_page_creates_signs_and_arms(dws, human):
    c = _client(dws)
    r = _new(c, "dark", type="feature")  # the type is forced to epic
    assert r.status_code == 303 and "err=" not in _loc(r)
    eid = re.search(r"/factory/([A-Z]+-\d+)", _loc(r)).group(1)
    t = _epic(dws, eid)
    assert t.meta["type"] == "epic" and t.status == "open"
    assert t.section("Requirements") == ASK  # the ask, byte for byte
    assert t.section("Acceptance criteria") == DONE and not t.section("Ask").strip()
    d = epics.delegation(dws, t)
    assert d["factory"] is True and d["dark"] is True and d["active"] and fs.armed(dws, d["id"])
    charter = [e for e in ledger.entries(dws) if e.get("kind") == "charter"][-1]
    assert charter["actor"].startswith("human") and charter["delegate"]["dark"] is True


def test_ai_factory_start_from_the_new_page(fws):
    c = _client(fws)
    eid = _started(c, fws, "factory")
    d = epics.delegation(fws, _epic(fws, eid))
    assert d["factory"] and not d.get("dark") and fs.armed(fws, d["id"])


def test_a_ticket_from_the_new_page_is_as_before(fws):
    c = _client(fws)
    r = _post(c, "/new", title="Plain one", mode="ticket", ask="just do it", done_when=DONE)
    assert "/t/" in _loc(r) and "err=" not in _loc(r)
    (e,) = store.scan(fws)
    t = store.read_ticket(e.path)
    assert t.meta["type"] == "feature" and t.status == "backlog" and t.section("Ask") == "just do it"
    assert not t.section("Acceptance criteria") and epics.delegation(fws, t) is None


@pytest.mark.parametrize("ws_kind, data, why", [
    ("dark", {"confirm_dark": ""}, "type dark"),
    ("dark", {"confirm_dark": "Dark"}, "type dark"),
    ("dark", {"confirm_dark": "yes"}, "type dark"),
    ("factory", {}, "Dark AI Factory is off"),  # Dark off: a posted Dark mode is refused
    ("off", {}, "AI Factory is switched off"),
    ("dark", {"mode": "darker"}, "unknown mode"),  # a forged mode
    ("off", {"mode": "factory"}, "AI Factory is switched off"),
    ("dark", {"ask": "  "}, "describe the work"),
    ("dark", {"done_when": ""}, "say when it is done"),
])
def test_a_start_the_switches_or_the_typed_word_do_not_allow_creates_nothing(configure, human, ws_kind, data, why):
    from orch.core.ops import Ops
    ws = configure(factory={"enabled": ws_kind != "off"})
    if ws_kind == "dark":
        Ops(ws, human).set_factory_dark(True)
    n = len(ledger.entries(ws))
    r = _new(_client(ws), **{"mode": "dark", **data})
    assert r.status_code == 422 and why in r.text
    assert store.scan(ws) == [] and len(ledger.entries(ws)) == n


def test_a_failed_start_after_creation_goes_to_the_epic_not_a_form_error(dws, monkeypatch):
    monkeypatch.setenv("ORCH_HARNESS", "test-agent")  # the dashboard process runs under an agent: no start
    r = _new(_client(dws), "dark")
    assert r.status_code == 303 and "err=" in _loc(r) and "/t/" in _loc(r)
    assert "but+starting+it+as+a+Dark+AI+Factory+failed" in _loc(r) and "agent+harness" in _loc(r)
    (e,) = store.scan(dws)
    assert epics.delegation(dws, store.read_ticket(e.path)) is None
    assert not [x for x in ledger.entries(dws) if x.get("kind") == "charter"]


def test_the_new_page_needs_the_cookie_and_the_origin(dws):
    from orch.dashboard.app import create_app
    app = create_app(dws, "tok")
    data = {"title": "x", "mode": "dark", "ask": ASK, "done_when": DONE, "confirm_dark": "dark"}
    assert TestClient(app).post("/new", data=data, follow_redirects=False).status_code == 401
    c = TestClient(app)
    c.get("/?token=tok")
    assert c.post("/new", data=data, headers={"origin": "http://evil.example"}, follow_redirects=False).status_code == 403
    assert store.scan(dws) == []


# -- the epic page: Start as a Dark AI Factory -------------------------------------------------------------------------

def test_the_epic_page_offers_dark_only_while_dark_is_on(fws, fa, human):
    e = fa.new("Epic", type="epic")
    _refine(fa, e.id, plan=None)
    assert 'name="dark"' not in _client(fws).get(f"/t/{e.id}").text
    seen = epics.charter(fws, _epic(fws, e.id))["content_hash"]
    r = _post(_client(fws), f"/t/{e.id}/approve", gate="requirements", seen=seen, dark="1")
    assert "err=" in _loc(r) and epics.delegation(fws, _epic(fws, e.id)) is None  # Dark off: refused
    from orch.core.ops import Ops
    Ops(fws, human).set_factory_dark(True)
    c = _client(fws)
    assert 'name="dark"' in c.get(f"/t/{e.id}").text and "Start as a Dark AI Factory" in c.get(f"/t/{e.id}").text
    r = _post(c, f"/t/{e.id}/approve", gate="requirements", seen=seen, dark="1")
    assert "err=" not in _loc(r)
    d = epics.delegation(fws, _epic(fws, e.id))
    assert d["factory"] and d["dark"] and fs.armed(fws, d["id"])


def test_the_epic_page_dark_start_is_refused_to_an_agent_harness(dws, fa, monkeypatch):
    e = fa.new("Epic", type="epic")
    _refine(fa, e.id, plan=None)
    seen = epics.charter(dws, _epic(dws, e.id))["content_hash"]
    c = _client(dws)
    monkeypatch.setenv("ORCH_HARNESS", "test-agent")
    r = _post(c, f"/t/{e.id}/approve", gate="requirements", seen=seen, dark="1")
    assert "err=" in _loc(r) and "agent+harness" in _loc(r) and epics.delegation(dws, _epic(dws, e.id)) is None


def test_the_confirm_label_names_a_dark_start():
    js = (__import__("pathlib").Path(__import__("orch").__file__).parent / "dashboard/static/app.js").read_text()
    assert "START DARK AI FACTORY" in js


# -- end to end: only the dashboard's start, then the runner, the hook and the card ------------------------------------

def test_end_to_end_dark_through_the_dashboard_start(dws, fa, human):
    c = _client(dws)
    eid = _started(c, dws, "dark")  # no direct arm: the New page's start armed the runner
    cid = _child(fa, eid)
    fake = Fake()
    assert _tick(dws, human, fake)[0].startswith("started")
    (b,) = fs.bindings(dws)
    d = epics.delegation(dws, _epic(dws, eid))
    assert (b["epic"], b["child"], b["delegation"]) == (eid, cid, d["id"]) and permits.dark_delegation(dws, _epic(dws, eid))
    dark_profile.add(dws, human, "prefix", "make test")
    pay = lambda cmd: {"session_id": b["session"], "tool_name": "Bash", "tool_input": {"command": cmd}}  # noqa: E731
    assert _behavior(permits.hook_decision(dws, pay("make test --quiet"))) == "allow"  # the profile covers it
    out = permits.hook_decision(dws, pay("make lint"))
    assert _behavior(out) == "deny" and "not in the Dark profile" in out["hookSpecificOutput"]["decision"]["message"]
    (r,) = permits.open_requests(dws)
    assert r["source"] == "dark" and r["command"] == "make lint"
    for url in ("/", f"/factory/{eid}"):
        card = c.get(url).text.split(f'data-permit="{r["id"]}"', 1)[1].split("</article>", 1)[0]
        assert "Add to the Dark profile" in card and f'action="/permits/{r["id"]}/profile"' in card
        assert "not in the Dark profile" in card and "Grant once" in card and "Deny" in card
    resp = _post(c, f"/permits/{r['id']}/profile", sha=r["sha"], next=f"/factory/{eid}")
    assert "err=" not in _loc(resp) and _loc(resp).startswith(f"/factory/{eid}")
    assert any(x["kind"] == "exact" and x["rule"] == "make lint" for x in dark_profile.rules(dws))
    assert permits.open_requests(dws) == [] and f'data-permit="{r["id"]}"' not in c.get(f"/factory/{eid}").text
    assert _behavior(permits.hook_decision(dws, pay("make lint"))) == "allow"
    assert b["session"] not in c.get(f"/factory/{eid}").text  # session ids never reach a page


# -- POST /permits/{rid}/profile refusals ------------------------------------------------------------------------------

@pytest.fixture
def dark_request(dws, fa, human):
    """A Dark epic started from the dashboard with one child and an open Dark card for CMD."""
    c = _client(dws)
    eid = _started(c, dws, "dark")
    cid = _child(fa, eid)
    r = permits.request(dws, fa.actor, _epic(dws, cid), CMD, reason="not in the Dark profile", source="dark")
    return c, eid, cid, r


def test_profile_post_refuses_a_stale_sha(dws, dark_request):
    c, eid, cid, r = dark_request
    n = len(ledger.entries(dws))
    for sha in ("", "sha256:" + "0" * 64):
        assert "err=" in _loc(_post(c, f"/permits/{r['id']}/profile", sha=sha))
    assert len(ledger.entries(dws)) == n and len(permits.open_requests(dws)) == 1


def test_profile_post_refuses_a_request_dark_did_not_file(dws, fa, dark_request):
    c, eid, cid, _ = dark_request
    r = permits.request(dws, fa.actor, _epic(dws, cid), "make other", source="agent")
    n = len(ledger.entries(dws))
    resp = _post(c, f"/permits/{r['id']}/profile", sha=r["sha"])
    assert "err=" in _loc(resp) and "not+filed+by+a+Dark+factory" in _loc(resp) and len(ledger.entries(dws)) == n


def test_profile_post_refuses_another_workspaces_request(dws, dark_request):
    c, eid, cid, r = dark_request
    body = permits._dir("requests") / f"{r['id']}.json"
    data = json.loads(body.read_text())
    body.write_text(json.dumps({**data, "workspace": "someone-else"}))  # filed by another workspace
    n = len(ledger.entries(dws))
    resp = _post(c, f"/permits/{r['id']}/profile", sha=r["sha"])
    assert "err=" in _loc(resp) and len(ledger.entries(dws)) == n and dark_profile.rules(dws) == []


def test_profile_post_is_the_humans_only(dws, dark_request, monkeypatch):
    c, eid, cid, r = dark_request
    from orch.dashboard.app import create_app
    assert TestClient(create_app(dws, "tok")).post(f"/permits/{r['id']}/profile", data={"sha": r["sha"]},
                                                   follow_redirects=False).status_code == 401
    assert _post(c, f"/permits/{r['id']}/profile", sha=r["sha"]).status_code == 303
    assert dark_profile.rules(dws)  # the human's own post works ...
    r2 = permits.request(dws, _agent(), _epic(dws, cid), "make two", source="dark")
    monkeypatch.setenv("ORCH_HARNESS", "test-agent")
    n = len(ledger.entries(dws))
    resp = _post(c, f"/permits/{r2['id']}/profile", sha=r2["sha"])
    assert "err=" in _loc(resp) and "agent+harness" in _loc(resp) and len(ledger.entries(dws)) == n


def _agent():
    from orch.core.events import Actor
    return Actor("agent", "claude-code", "cli", "7f3c9a21-0000")


def test_a_card_from_an_ordinary_factory_has_no_profile_button(fws, fa, fh):
    e = fa.new("Epic", type="epic")
    _refine(fa, e.id, plan=None)
    fh.approve(e.id, "requirements", delegate={"factory": True})
    cid = _child(fa, e.id)
    r = permits.request(fws, fa.actor, _epic(fws, cid), CMD)
    card = _client(fws).get("/").text.split(f'data-permit="{r["id"]}"', 1)[1].split("</article>", 1)[0]
    assert "Add to the Dark profile" not in card and "Grant once" in card


# -- the run view -----------------------------------------------------------------------------------------------------

def _ring(html):
    return html.split('class="ring"', 1)[1].split("</svg>", 1)[0]


def test_run_view_working_dark(dws, fa):
    c = _client(dws)
    eid = _started(c, dws, "dark")
    html = c.get(f"/factory/{eid}").text
    assert "ring-panel is-dark" in html and " hot" not in html.split("ring-panel", 1)[1].split(">", 1)[0]
    assert 'aria-valuetext="Step 1 of 5: Understand' in html  # no child yet: not even Understand is done
    _child(fa, eid)
    html = c.get(f"/factory/{eid}").text
    assert "Dark AI Factory is working" in html and 'role="status"' in html and 'role="progressbar"' in html
    assert 'aria-valuenow="2"' in html and 'aria-valuetext="Step 3 of 5: Build, in progress"' in html
    assert "ring-panel is-dark hot" in html  # the run reached real work: the glow is stronger
    ring = _ring(html)
    assert ring.count("is-done") == 2 and ring.count("is-now") == 1 and "ring-run" not in ring and "ring-core" in ring
    assert "No estimate yet." in html and "Running for" in html and "Stop the run…" in html
    for gone in ("Test", "Review", "Merge", "Release"):
        assert f">{gone}<" not in html


def test_run_view_working_ai_factory(fws, fa):
    c = _client(fws)
    eid = _started(c, fws, "factory")
    _child(fa, eid)
    html = c.get(f"/factory/{eid}").text
    assert "AI Factory is working" in html and "is-dark" not in html and "ring-run" in _ring(html)
    assert "ring-core" not in html


def test_run_view_waiting_for_you_shows_the_card(dws, fa, dark_request):
    c, eid, cid, r = dark_request
    html = c.get(f"/factory/{eid}").text
    assert "Waiting for you" in html and f'data-permit="{r["id"]}"' in html and "is-wait" in _ring(html)
    assert f'name="next" value="/factory/{eid}"' in html


def test_run_view_paused_after_stop_the_run(dws, fa):
    c = _client(dws)
    eid = _started(c, dws, "dark")
    _child(fa, eid)
    resp = _post(c, f"/t/{eid}/epic/pause", next=f"/factory/{eid}")
    assert "err=" not in _loc(resp) and _loc(resp).startswith(f"/factory/{eid}")
    html = c.get(f"/factory/{eid}").text
    assert "Paused: you stopped the run" in html and "is-stop" in _ring(html) and "Stop the run…" not in html


def test_run_view_stopped_and_budget_used_up(dws, fa, human, dark_request, monkeypatch):
    from orch import clock
    c, eid, cid, r = dark_request
    permits.permit_deny(dws, human, r["id"], expected_sha=r["sha"])
    html = c.get(f"/factory/{eid}").text
    assert "data-stopped=" in html and "Permission denied" in html and "is-stop" in _ring(html)
    assert re.search(r'chip-warn">.*?stopped</span> Stopped</h2>', html, re.S)
    real = clock.now()
    monkeypatch.setattr(clock, "now", lambda: real + timedelta(hours=73))
    html = c.get(f"/factory/{eid}").text
    assert "Stopped" in html and "time budget of 72 hours used up" in html


def test_run_view_only_budget_says_budget_used_up(dws, fa, monkeypatch):
    from orch import clock
    c = _client(dws)
    eid = _started(c, dws, "dark")
    _child(fa, eid)
    real = clock.now()
    monkeypatch.setattr(clock, "now", lambda: real + timedelta(hours=73))
    assert "Budget used up" in c.get(f"/factory/{eid}").text.split("</h2>", 1)[0]


def test_run_view_runner_blocked_and_unarmed(dws, fa, fh, monkeypatch):
    c = _client(dws)
    eid = _started(c, dws, "dark")
    _child(fa, eid)
    monkeypatch.setattr(factory_runner, "user_settings_blocker", lambda environ=None: "the orch hooks are missing")
    html = c.get(f"/factory/{eid}").text
    assert "The runner starts nothing" in html and "the orch hooks are missing" in html
    e2 = fa.new("Terminal start", type="epic")
    _refine(fa, e2.id, plan=None)
    fh.approve(e2.id, "requirements", delegate={"factory": True})  # the terminal's start arms nothing
    assert "Not running: it was started in a terminal" in c.get(f"/factory/{e2.id}").text


def test_run_view_evidence_then_finished_with_a_summary(fws, fa, fh, close_tasks):
    e = fa.new("Billing revamp", type="epic")
    _refine(fa, e.id, plan=None)
    fh.approve(e.id, "requirements", delegate={"factory": True})
    cid = _child(fa, e.id)
    fa.claim(cid)
    c = _client(fws)
    assert 'aria-valuetext="Step 3 of 5: Build' in c.get(f"/factory/{e.id}").text  # a task still open
    close_tasks(fa, cid)
    fa.set_section(cid, "Verification", "- AC1: ran the full suite, green")
    fa.move(cid, "testing")
    html = c.get(f"/factory/{e.id}").text
    assert 'aria-valuetext="Step 5 of 5: Done, waiting for you"' in html and "data-ready=" in html
    seen = factory_report.ready(fws, _epic(fws, e.id))["seen"]
    assert "err=" not in _loc(_post(c, f"/t/{e.id}/verdict", verdict="done", seen=seen, next=f"/factory/{e.id}"))
    html = c.get(f"/factory/{e.id}").text
    assert "Finished" in html
    assert 'aria-valuenow="5"' in html and _ring(html).count("is-done") == 5
    summary = html.split('id="run-summary-h"', 1)[1].split("</section>", 1)[0]
    assert "1 child, 1 task done" in summary and "0 permission requests" in summary and "from your start to your verdict" in summary
    assert "Stop the run…" not in html and "Ran for" in html


def test_run_view_steps_come_from_records_not_ticket_text(fws, fa, fh):
    e = fa.new("Epic", type="epic")
    _refine(fa, e.id, plan=None)
    fh.approve(e.id, "requirements", delegate={"factory": True})
    cid = _child(fa, e.id)
    path, t = store.load(fws, cid)
    t.meta["status"] = "done"  # forged: no signed verdict behind it
    store.save(fws, t, path)
    html = _client(fws).get(f"/factory/{e.id}").text
    assert 'aria-valuenow="1"' in html and "Finished" not in html and "Not verifiable" in html


def test_run_view_escapes_the_title_and_keeps_command_text_out_of_the_log(dws, fa, dark_request):
    c, eid, cid, r = dark_request
    path, t = store.load(dws, eid)
    t.meta["title"] = "<script>alert(1)</script>"
    store.save(dws, t, path)
    html = c.get(f"/factory/{eid}").text
    assert "<script>alert(1)</script>" not in html and "&lt;script&gt;alert(1)&lt;/script&gt;" in html
    log = html.split('class="card run-log"', 1)[1]
    assert "permit.requested" in log and "make" not in log and "deploy" not in log and r["sha"][7:] not in log
    assert "7f3c9a21" not in log  # no agent session id
    lst = c.get("/factory").text
    assert "<script>alert(1)</script>" not in lst and "&lt;script&gt;" in lst


def test_run_view_is_absent_for_a_plain_epic_and_with_the_factory_off(fws, fa, configure):
    e = fa.new("Plain", type="epic")
    assert _client(fws).get(f"/factory/{e.id}").status_code == 404
    off = configure(factory={"enabled": False})
    c = _client(off)
    assert c.get("/factory").status_code == 404 and c.get(f"/factory/{e.id}").status_code == 404


# -- the factory list ---------------------------------------------------------------------------------------------------

def test_factory_list_order_counts_and_menu(dws, fa, human, close_tasks):
    c = _client(dws)
    working = _started(c, dws, "dark")
    _child(fa, working)
    waiting = _started(c, dws, "factory")
    w_child = _child(fa, waiting)
    permits.request(dws, fa.actor, _epic(dws, w_child), CMD)
    stopped = _started(c, dws, "factory")
    _child(fa, stopped)
    _post(c, f"/t/{stopped}/epic/pause")
    finished = _started(c, dws, "factory")
    f_child = _child(fa, finished)
    fa.claim(f_child)
    close_tasks(fa, f_child)
    fa.set_section(f_child, "Verification", "- AC1: ran it, green")
    fa.move(f_child, "testing")
    seen = factory_report.ready(dws, _epic(dws, finished))["seen"]
    assert "err=" not in _loc(_post(c, f"/t/{finished}/verdict", verdict="done", seen=seen))
    html = c.get("/factory").text
    assert "4 factories, 1 working, 1 needs you" in html
    order = re.findall(r'data-factory="([A-Z]+-\d+)"', html)
    assert order == [waiting, working, stopped, finished]
    row = html.split(f'data-factory="{working}"', 1)[1].split("</li>\n", 1)[0]
    assert ">Dark<" in row and "chip-info" in row and row.count('class="step-mark is-') == 5
    assert 'href="/factory"' in html and ">Factories<" in html


def test_factory_list_empty_state_and_no_menu_entry_while_off(fws, configure):
    html = _client(fws).get("/factory").text
    assert "No factories yet" in html and "0 factories, 0 working, 0 need you" in html
    assert 'href="/factory"' not in _client(configure(factory={"enabled": False})).get("/").text
