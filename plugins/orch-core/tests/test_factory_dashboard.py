"""AI Factory, phase 2 (#2): the dashboard surface. Permission cards on Today and in Your move, Grant once / for this
epic / Deny / Revoke, Start on the epic page, the factory status, the budget card and the Board group. Everything is
the human's: an agent harness, a missing cookie and a cross-origin post are refused, and nothing shows while the
factory is off."""
import re
from datetime import timedelta

import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient  # noqa: E402

from orch.core import epics, ledger, permits, store  # noqa: E402

CMD = "make <deploy> 'staging'"  # printable ASCII with HTML-special characters


def _refine(ops, tid, plan="1. do it"):
    ops.set_section(tid, "Requirements", "r")
    ops.set_section(tid, "Acceptance criteria", "- [ ] a")
    if plan:
        ops.set_section(tid, "Plan", plan)
    return tid


@pytest.fixture
def fws(configure):
    return configure(factory={"enabled": True})


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


@pytest.fixture
def fd(fws):
    return _client(fws)


@pytest.fixture
def running(fws, fa, fh):
    """A started factory epic with one claimed child that asked for CMD."""
    e = fa.new("Billing revamp", type="epic")
    _refine(fa, e.id, plan=None)
    fh.approve(e.id, "requirements", delegate={"factory": True})
    c = fa.new("child", epic=e.id)
    _refine(fa, c.id)
    fa.epic_auto_approve(c.id)
    fa.claim(c.id)
    r = permits.request(fws, fa.actor, store.load(fws, c.id)[1], CMD, reason="deploy the <preview>")
    return e.id, c.id, r


def _posts(c, url, **data):
    return c.post(url, data=data, follow_redirects=False)


def _msg(resp):
    return resp.headers["location"]


# -- off by default ------------------------------------------------------------------------------------------------

def test_nothing_shows_while_the_factory_is_off(ws, aops, dash):
    e = aops.new("Plain epic", type="epic")
    _refine(aops, e.id, plan=None)
    for url in ("/", "/board", f"/t/{e.id}"):
        html = dash.get(url).text
        assert "AI Factory" not in html and "permit-card" not in html and 'name="factory"' not in html
    assert '<option value="factory"' not in dash.get("/board").text
    assert "Factory epic" not in dash.get("/board?group=factory").text  # the group is not offered, falls back to none


def test_answering_is_refused_while_the_factory_is_off(ws, dash):
    r = _posts(dash, "/permits/P-00000000/grant", sha="sha256:x", scope="once")
    assert "err=" in _msg(r) and not ledger.entries(ws)


# -- Start ---------------------------------------------------------------------------------------------------------

def test_epic_page_offers_start_and_it_signs_the_factory_charter(fws, fa, fd):
    e = fa.new("Billing revamp", type="epic")
    _refine(fa, e.id, plan=None)
    page = fd.get(f"/t/{e.id}").text
    assert 'name="factory"' in page and "Not started as a factory" in page
    seen = epics.charter(fws, store.load(fws, e.id)[1])["content_hash"]
    r = _posts(fd, f"/t/{e.id}/approve", gate="requirements", seen=seen, factory="1", delegate="1", max_children="3")
    assert "err=" not in _msg(r)
    d = epics.delegation(fws, store.load(fws, e.id)[1])
    assert d["factory"] and d["max_children"] == 25 and d["max_hours"] == 72  # the factory's own limits win
    page = fd.get(f"/t/{e.id}").text
    assert "running" in page and "0/25" in page and "72.0</b> of 72 hours left" in page


def test_start_is_refused_with_the_factory_off(ws, aops, dash):
    e = aops.new("Epic", type="epic")
    _refine(aops, e.id, plan=None)
    seen = epics.charter(ws, store.load(ws, e.id)[1])["content_hash"]
    r = _posts(dash, f"/t/{e.id}/approve", gate="requirements", seen=seen, factory="1")
    assert "err=" in _msg(r)
    assert epics.delegation(ws, store.load(ws, e.id)[1]) is None


def test_start_needs_the_hash_the_page_showed(fws, fa, fd):
    e = fa.new("Billing revamp", type="epic")
    _refine(fa, e.id, plan=None)
    r = _posts(fd, f"/t/{e.id}/approve", gate="requirements", seen="", factory="1")
    assert "err=" in _msg(r) and epics.delegation(fws, store.load(fws, e.id)[1]) is None


def test_confirm_label_says_it_starts_a_factory():
    js = (__import__("pathlib").Path(__import__("orch").__file__).parent / "dashboard/static/app.js").read_text()
    assert "START AI FACTORY" in js


# -- the cards -----------------------------------------------------------------------------------------------------

def test_cards_show_the_exact_escaped_command_reason_and_epic(fws, fd, running):
    eid, cid, r = running
    for url, back in (("/", "/"), ("/board", "/board")):
        html = fd.get(url).text
        card = html.split(f'data-permit="{r["id"]}"', 1)[1].split("</article>", 1)[0]
        assert "make &lt;deploy&gt; &#39;staging&#39;" in card  # escaped, never markup
        assert "deploy the &lt;preview&gt;" in card and f'href="/t/{eid}"' in card and f'href="/t/{cid}"' in card
        for label in ("Grant once", "Grant for this epic", "Deny"):
            assert label in card
        assert f'name="sha" value="{r["sha"]}"' in card and f'name="next" value="{back}"' in card
        assert "confirm(" not in card and "onclick" not in card
    assert "<deploy>" not in fd.get("/").text


def test_grant_once_signs_into_the_ledger_and_answers_the_hook(fws, fd, running):
    eid, cid, r = running
    before = len(ledger.entries(fws))
    resp = _posts(fd, f"/permits/{r['id']}/grant", sha=r["sha"], scope="once", next="/")
    assert "err=" not in _msg(resp) and len(ledger.entries(fws)) == before + 1
    g = ledger.entries(fws)[-1]
    assert g["kind"] == "grant" and g["scope"] == "once" and g["command"] == CMD and g["actor"].startswith("human")
    assert not permits.open_requests(fws) and "permit-card" not in fd.get("/").text
    out = permits.hook_decision(fws, {"session_id": "7f3c9a21-0000", "tool_name": "Bash",
                                      "tool_input": {"command": CMD}})
    assert out["hookSpecificOutput"]["decision"]["behavior"] == "allow"


def test_grant_for_the_epic_then_revoke_from_the_epic_page(fws, fd, running):
    eid, cid, r = running
    _posts(fd, f"/permits/{r['id']}/grant", sha=r["sha"], scope="epic", next=f"/t/{eid}")
    page = fd.get(f"/t/{eid}").text
    assert "Standing grants" in page and "for this epic" in page and "make &lt;deploy&gt;" in page
    (g,) = [x for x in permits.grants(fws) if x["live"]]
    assert f"/permits/grants/{g['grant']}/revoke" in page
    resp = _posts(fd, f"/permits/grants/{g['grant']}/revoke", next=f"/t/{eid}")
    assert "err=" not in _msg(resp)
    assert not [x for x in permits.grants(fws) if x["live"]]
    assert "Standing grants" not in fd.get(f"/t/{eid}").text
    assert ledger.entries(fws)[-1]["kind"] == "permit_revoke"


def test_deny_closes_the_card_with_a_signed_no(fws, fd, running):
    eid, cid, r = running
    resp = _posts(fd, f"/permits/{r['id']}/deny", sha=r["sha"], next="/")
    assert "err=" not in _msg(resp) and ledger.entries(fws)[-1]["kind"] == "permit_deny"
    assert not permits.open_requests(fws)


def test_an_answer_is_bound_to_the_command_the_card_showed(fws, fd, running):
    eid, cid, r = running
    n = len(ledger.entries(fws))
    for url in (f"/permits/{r['id']}/grant", f"/permits/{r['id']}/deny"):
        for sha in ("", "sha256:" + "0" * 64):
            assert "err=" in _msg(_posts(fd, url, sha=sha, scope="once"))
    assert len(ledger.entries(fws)) == n and len(permits.open_requests(fws)) == 1
    assert "err=" in _msg(_posts(fd, f"/permits/{r['id']}/grant", sha=r["sha"], scope="forever"))
    assert len(ledger.entries(fws)) == n


def test_the_budget_card_shows_when_the_time_is_used_up(fws, fa, fd, running, monkeypatch):
    from orch import clock
    eid, cid, r = running
    real = clock.now()
    monkeypatch.setattr(clock, "now", lambda: real + timedelta(hours=73))
    for url in ("/", "/board", f"/t/{eid}"):
        html = fd.get(url).text
        assert "Budget used up" in html and "time budget of 72 hours used up" in html
    assert "budget used up" in fd.get(f"/t/{eid}").text
    assert 'data-budget' not in fd.get(f"/t/{cid}").text


def test_the_epic_page_shows_its_children_budget(fws, fa, fd, running):
    eid, cid, r = running
    page = fd.get(f"/t/{eid}").text
    assert "1/25" in page and "running" in page and 'id="factory"' in page


# -- the Board -----------------------------------------------------------------------------------------------------

def test_board_groups_by_factory_epic(fws, fa, fd, running):
    eid, cid, r = running
    other = fa.new("Ordinary epic", type="epic")
    fa.new("Loose ticket")
    html = fd.get("/board?group=factory").text
    assert '<option value="factory" selected>' in html
    lanes = re.findall(r'<h2 class="lane-h"[^>]*>(.*?)</h2>', html, re.S)
    assert any("Billing revamp" in x for x in lanes) and any("Not in a factory" in x for x in lanes)
    assert not any("Ordinary epic" in x for x in lanes)
    assert other.id in html  # still on the board, in the group without a factory
    fd.get("/board?group=none")


# -- only the human ------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("what", ["grant", "deny", "revoke", "start"])
def test_an_agent_harness_cannot_answer_or_start(fws, fa, fh, fd, running, monkeypatch, what):
    eid, cid, r = running
    g = permits.permit_grant(fws, fh.actor, r["id"], "epic", expected_sha=r["sha"]) if what == "revoke" else None
    if what == "start":
        e2 = fa.new("Second epic", type="epic")
        _refine(fa, e2.id, plan=None)
        seen = epics.charter(fws, store.load(fws, e2.id)[1])["content_hash"]
    monkeypatch.setenv("ORCH_HARNESS", "test-agent")  # the dashboard process itself runs under an agent
    n = len(ledger.entries(fws))
    if what == "grant":
        resp = _posts(fd, f"/permits/{r['id']}/grant", sha=r["sha"], scope="once")
    elif what == "deny":
        resp = _posts(fd, f"/permits/{r['id']}/deny", sha=r["sha"])
    elif what == "revoke":
        resp = _posts(fd, f"/permits/grants/{g['grant']}/revoke")
    else:
        resp = _posts(fd, f"/t/{e2.id}/approve", gate="requirements", seen=seen, factory="1")
    assert "err=" in _msg(resp) and "agent+harness" in _msg(resp)
    assert len(ledger.entries(fws)) == n
    if what == "start":
        assert epics.delegation(fws, store.load(fws, e2.id)[1]) is None
    if what in ("grant", "deny"):
        assert len(permits.open_requests(fws)) == 1


def test_the_new_routes_need_the_dashboards_cookie_and_origin(fws, running):
    eid, cid, r = running
    urls = [(f"/permits/{r['id']}/grant", {"sha": r["sha"], "scope": "once"}),
            (f"/permits/{r['id']}/deny", {"sha": r["sha"]}), ("/permits/grants/0123456789abcdef/revoke", {})]
    from orch.dashboard.app import create_app
    app = create_app(fws, "tok")
    n = len(ledger.entries(fws))
    for url, data in urls:
        assert TestClient(app).post(url, data=data, follow_redirects=False).status_code == 401  # no cookie
        c = TestClient(app)
        c.get("/?token=tok")
        assert c.post(url, data=data, headers={"origin": "http://evil.example"},
                      follow_redirects=False).status_code == 403
    assert len(ledger.entries(fws)) == n and len(permits.open_requests(fws)) == 1


def test_an_agent_request_cannot_close_or_answer_its_own_card(fws, fa, fd, running):
    eid, cid, r = running
    permits.request(fws, fa.actor, store.load(fws, cid)[1], CMD)  # the same card, not a second
    assert len(permits.open_requests(fws)) == 1
    assert not [g for g in permits.grants(fws)]


def test_a_card_shows_the_raw_command_the_grant_binds(fws, fa, fd, running):
    eid, cid, _ = running
    cmd = 'echo "a\\b" > f'
    r = permits.request(fws, fa.actor, store.load(fws, cid)[1], cmd)
    card = fd.get("/").text.split(f'data-permit="{r["id"]}"', 1)[1].split("</article>", 1)[0]
    assert "echo &#34;a\\b&#34; &gt; f" in card and "\\\\" not in card  # quotes and backslash as written


def test_an_unused_once_grant_is_listed_with_revoke(fws, fd, running):
    eid, cid, r = running
    _posts(fd, f"/permits/{r['id']}/grant", sha=r["sha"], scope="once", next=f"/t/{eid}")
    page = fd.get(f"/t/{eid}").text
    assert "once, unused" in page and "/revoke" in page


def test_start_names_the_limits_from_the_defaults(fws, fa, fd):
    e = fa.new("Epic", type="epic")
    _refine(fa, e.id, plan=None)
    page = fd.get(f"/t/{e.id}").text
    assert 'data-factory-confirm="up to 25 children or 72 hours, size ≤ m"' in page
