"""E2 dashboard: Group by on Board and List (remembered per workspace), the epic link on cards, the epic page with
its charter approval and delegation, and Today's grouping by epic with the delegation FYI."""
import json
import re

import pytest

pytest.importorskip("fastapi")

from orch.core import epics, store  # noqa: E402


def _refine(ops, tid, plan="1. do it"):
    ops.set_section(tid, "Requirements", "r")
    ops.set_section(tid, "Acceptance criteria", "- [ ] a")
    if plan:
        ops.set_section(tid, "Plan", plan)
    return tid


@pytest.fixture
def epic(aops):
    e = aops.new("Billing revamp", type="epic")
    _refine(aops, e.id, plan=None)
    c = aops.new("Invoice export", epic=e.id)
    _refine(aops, c.id)
    return e.id, c.id


def test_group_by_epic_on_the_board_is_remembered(dash, ws, aops, epic):
    eid, cid = epic
    aops.new("Loose ticket")
    html = dash.get("/board?group=epic").text
    lanes = re.findall(r'<h2 class="lane-h"[^>]*>(.*?)</h2>', html, re.S)
    assert any("Billing revamp" in x for x in lanes) and any("No epic" in x for x in lanes)
    assert '<span class="filter-label">Group by</span>' in html  # a visible label, not only an aria-label
    assert re.findall(r'<h2 class="lane-h"', dash.get("/board").text)  # remembered for this workspace
    assert not re.findall(r'<h2 class="lane-h"', dash.get("/board?group=none").text)
    assert not re.findall(r'<h2 class="lane-h"', dash.get("/board").text)


@pytest.mark.parametrize("by,label", [("label", "No label"), ("agent", "No agent"), ("repo", "No repo"),
                                      ("sprint", "No sprint")])
def test_group_by_on_the_list(dash, ws, aops, by, label):
    aops.new("Plain")
    html = dash.get(f"/board?view=list&group={by}").text
    assert re.search(r'class="list-group-h"[^>]*>[^<]*' + label, html)


def test_group_by_sprint_uses_the_config(configure, aops):
    ws = configure(sprints=[{"id": "S1", "name": "Sprint one", "start": "2026-10-01", "end": "2026-10-14"}])
    from fastapi.testclient import TestClient
    from orch.core.ops import Ops
    from orch.dashboard.app import create_app
    Ops(ws, aops.actor).new("Planned", sprint="S1")
    client = TestClient(create_app(ws, "tok"))
    client.get("/?token=tok")
    html = client.get("/board?group=sprint").text
    assert "Sprint one" in html


def test_card_and_header_link_the_epic_above_the_title(dash, ws, aops, epic):
    eid, cid = epic
    html = dash.get("/board").text
    card = html.split(f'data-ticket="{cid}"', 1)[1].split("</a>", 1)[0]
    assert "Billing revamp" in card and card.index("tc-epic") < card.index('class="title"')  # M: an epic chip
    page = dash.get(f"/t/{cid}").text
    head = page.split('<div class="tc-header">', 1)[1]
    assert f'href="/t/{eid}"' in head.split("<h1>", 1)[0]


def test_epic_page_lists_children_and_approves_the_charter(dash, ws, aops, epic):
    eid, cid = epic
    page = dash.get(f"/t/{eid}").text
    assert 'id="epic-children"' in page and f'href="/t/{cid}"' in page
    charter = epics.charter(ws, store.load(ws, eid)[1])
    assert f'name="seen" value="{charter["content_hash"]}"' in page
    assert 'name="delegate"' in page
    assert "<li>do it</li>" in page.split('class="charter-plan"', 1)[1]  # the child's plan is in the approve view
    r = dash.post(f"/t/{eid}/approve", data={"gate": "requirements", "seen": charter["content_hash"], "delegate": "1",
                                             "max_children": "3", "max_size": "s"}, follow_redirects=False)
    assert r.status_code == 303 and "err=" not in r.headers["location"]
    d = epics.delegation(ws, store.load(ws, eid)[1])
    assert d["active"] and d["max_children"] == 3 and d["max_size"] == "s"
    assert store.load(ws, cid)[1].status == "open"
    page = dash.get(f"/t/{eid}").text
    assert "Pause delegation" in page and "covered by the epic approval" in page
    r = dash.post(f"/t/{eid}/epic/pause", follow_redirects=False)
    assert r.status_code == 303 and "err=" not in r.headers["location"]
    assert epics.delegation(ws, store.load(ws, eid)[1])["paused"]


def test_stale_charter_hash_is_refused(dash, ws, aops, epic):
    eid, cid = epic
    seen = epics.charter(ws, store.load(ws, eid)[1])["content_hash"]
    _refine(aops, aops.new("late", epic=eid).id)
    r = dash.post(f"/t/{eid}/approve", data={"gate": "requirements", "seen": seen}, follow_redirects=False)
    assert "err=" in r.headers["location"]
    assert store.load(ws, eid)[1].status == "backlog"


def test_today_groups_by_epic_and_sends_the_charter_to_the_epic_page(dash, ws, aops, hops, epic):
    eid, cid = epic
    # backlog: the epic's first approval is grooming; the one-by-one view links to the epic page, never approves
    groom = dash.get(f"/groom?at={eid}").text
    assert f'href="/t/{eid}#epic-approve"' in groom and f'action="/t/{eid}/approve"' not in groom
    hops.approve(eid, "requirements")
    aops.claim(cid)
    aops.set_section(cid, "Plan", "1. a different plan")  # the epic needs the human again
    loose = aops.new("Loose")
    _refine(aops, loose.id)
    hops.approve(loose.id, "requirements")
    aops.claim(loose.id)
    aops.set_section(loose.id, "Plan", "1. plan")  # an ungrouped blocking decision
    html = dash.get("/").text
    group = html.split(f'data-group="{eid}" role="group"', 1)[1].split('<div class="dc-group"', 1)[0]
    assert "Billing revamp" in group and f'href="/t/{eid}#epic-approve"' in group
    assert f'action="/t/{eid}/approve"' not in html
    card = html.split(f'id="d-{eid}-approve-epic"', 1)[1].split("</article>", 1)[0]
    assert "data-key-primary" in card and "<form" not in card  # the keyboard only opens the epic page


def test_today_shows_auto_approvals_as_quiet_fyi(dash, ws, aops, hops, epic):
    eid, _ = epic
    hops.approve(eid, "requirements", delegate={})
    auto = aops.new("Auto child", epic=eid)
    _refine(aops, auto.id)
    aops.epic_auto_approve(auto.id)
    html = dash.get("/").text
    fyi = html.split('id="delegated-fyi"', 1)[1].split("</section>", 1)[0]
    assert f'href="/t/{auto.id}"' in fyi and "Auto child" in fyi
    assert "Nothing is waiting on you" in html  # not counted as a decision


def test_epic_page_audits_auto_approvals_and_offers_the_epic_verdict(dash, ws, aops, hops, epic, close_tasks):
    eid, cid = epic
    hops.approve(eid, "requirements", delegate={})
    auto = _refine(aops, aops.new("Auto child", epic=eid, size="s").id)
    aops.epic_auto_approve(auto)
    page = dash.get(f"/t/{eid}").text
    audit = page.split('id="epic-audit"', 1)[1].split("</section>", 1)[0]
    assert auto in audit and "auto-approved" in audit
    for tid in (cid, auto):
        aops.claim(tid)
        close_tasks(aops, tid)
        aops.set_section(tid, "Verification", "- AC1: ran it")
        aops.move(tid, "testing")
    page = dash.get(f"/t/{eid}").text
    assert 'id="epic-verdict"' in page
    seen = re.search(r'name="seen" value="([^"]+)"', page.split('id="epic-verdict"', 1)[1]).group(1)
    r = dash.post(f"/t/{eid}/verdict", data={"verdict": "done"}, follow_redirects=False)
    assert "err=" in r.headers["location"]  # an epic verdict without what was read is refused
    aops.set_section(auto, "Verification", "- AC1: ran it, other evidence")
    r = dash.post(f"/t/{eid}/verdict", data={"verdict": "done", "seen": seen}, follow_redirects=False)
    assert "err=" in r.headers["location"]  # stale
    assert store.load(ws, cid)[1].status == "testing"
    page = dash.get(f"/t/{eid}").text
    seen = re.search(r'name="seen" value="([^"]+)"', page.split('id="epic-verdict"', 1)[1]).group(1)
    r = dash.post(f"/t/{eid}/verdict", data={"verdict": "done", "seen": seen}, follow_redirects=False)
    assert "err=" not in r.headers["location"]
    assert {store.load(ws, x)[1].status for x in (eid, cid, auto)} == {"done"}


def test_group_pref_is_outside_the_workspace(dash, ws, aops):
    from orch.dashboard import prefs
    dash.get("/board?group=label")
    assert prefs.group_by(ws) == "label"
    assert ws.root not in prefs.prefs_path().parents
    assert "label" not in json.dumps([p.read_text() for p in ws.tickets_dir.rglob("*.md")])


def test_charter_confirm_label_names_the_delegation(dash, ws, aops, epic):
    eid, _ = epic
    page = dash.get(f"/t/{eid}").text
    form = page.split('id="epic-approve"', 1)[1]
    # the dialog (confirm.js) is built from the fields when pressed: the epic and its hash are named in the form
    assert 'data-confirm-build="start" data-confirm-epic="epic ' in form and "inline-confirm=" not in form.split("</form>", 1)[0]
    assert 'autocomplete="off" data-confirm-build' in form  # the browser does not restore the delegation fields


def test_today_fyi_counts_and_links_the_rest(dash, ws, aops, hops, epic):
    eid, _ = epic
    hops.approve(eid, "requirements", delegate={"max_children": 12, "max_size": "m"})
    for i in range(10):
        _refine(aops, aops.new(f"Auto {i}", epic=eid).id)
        aops.epic_auto_approve(f"L-{3 + i:04d}")
    fyi = dash.get("/").text.split('id="delegated-fyi"', 1)[1].split("</section>", 1)[0]
    assert '<span class="count">10</span>' in fyi
    assert f'href="/t/{eid}#epic-audit">+2 more in {eid}' in fyi


def test_group_pref_is_not_rewritten_when_unchanged(dash, ws):
    from orch.dashboard import prefs
    dash.get("/board?group=epic")
    stamp = prefs.prefs_path().stat().st_mtime_ns
    dash.get("/board?group=epic")
    dash.get("/board")
    assert prefs.prefs_path().stat().st_mtime_ns == stamp


def _reads(dash, url):
    """How often a GET scans the tickets, reads the event log and reads the ledger."""
    from orch.core import events as events_mod
    from orch.core import ledger
    counts = {"scan": 0, "events": 0, "ledger": 0}
    mp = pytest.MonkeyPatch()

    def wrap(mod, name, key):
        real = getattr(mod, name)

        def counted(*a, **kw):
            counts[key] += 1
            return real(*a, **kw)
        mp.setattr(mod, name, counted)
    wrap(store, "_scan", "scan")
    wrap(events_mod, "read_events", "events")
    wrap(ledger, "entries", "ledger")
    try:
        assert dash.get(url).status_code == 200
    finally:
        mp.undo()
    return counts


@pytest.mark.parametrize("url", ["/", "/board?group=epic", "/t/L-0001"])
def test_reads_per_request_do_not_grow_with_the_children(dash, ws, aops, hops, url):
    e = aops.new("Big epic", type="epic")
    _refine(aops, e.id, plan=None)
    _refine(aops, aops.new("c0", epic=e.id).id)
    hops.approve(e.id, "requirements", delegate={})

    def add(n):
        for i in range(n):
            c = aops.new(f"auto {i}", epic=e.id)
            _refine(aops, c.id)
            aops.epic_auto_approve(c.id)
        aops.set_section("L-0002", "Plan", "1. changed")  # one child needs the epic again
    add(1)
    few = _reads(dash, url)
    add(5)
    many = _reads(dash, url)
    assert many == few, (few, many)


def test_epic_page_uses_the_bound_forms_and_no_popups(dash, ws, aops, epic):
    eid, cid = epic
    aops.ask(cid, [{"text": "Which format?", "options": [{"label": "CSV"}, {"label": "XLSX"}], "recommended": "A"}])
    page = dash.get(f"/t/{eid}").text
    approve = page.split('id="epic-approve"', 1)[1]
    rc = approve.split('action="/t/' + eid + '/request-changes"', 1)[1].split("</form>", 1)[0]
    assert 'name="seen" value="sha256:' in rc  # request changes binds the text shown
    assert "data-confirm=" not in page  # no browser popups on the epic page
    child = dash.get(f"/t/{cid}").text
    assert 'name="qhash"' in child  # a child's question is answered bound to its hash



def test_epic_page_in_a_move_state_renders_request_changes_once(dash, ws, aops, epic, monkeypatch):
    """A "Move back to backlog" bar (plus a hint line on the same gate) above the charter form: the requirements'
    "Request changes" form is rendered once, so its ids stay unique."""
    from orch.dashboard import routes_ticket
    eid, _ = epic
    move = {"kind": "human", "text": "Requirements changed after approval.",
            "action": {"kind": "move", "gate": "requirements", "to": "backlog", "label": "Move back to backlog"},
            "more": [{"text": "And a hint.", "action": {"kind": "hint", "gate": "requirements"}}]}
    monkeypatch.setattr(routes_ticket, "your_move", lambda *a, **k: move)
    page = dash.get(f"/t/{eid}").text
    assert "Move back to backlog" in page and 'id="epic-approve"' in page
    assert page.count('id="requirements-changes"') == 1
    assert page.count('id="requirements-changes-message"') == 1
