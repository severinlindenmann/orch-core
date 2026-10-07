"""Dark AI Factory on the dashboard (phase 5): the New ticket modes, the Dark start on the epic page, the run view, the
factory list and "Add to the Dark profile" on a card. Everything that starts or signs is the human's: a forged mode,
a missing typed word, Dark off, a second submit of the same form, an agent harness, a missing cookie and a
cross-origin post are refused, and a start the text would make fail creates nothing."""
import json
import os
import re
from datetime import timedelta
from pathlib import Path

import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient  # noqa: E402

import orch  # noqa: E402
from orch.core import dark_profile, epics, factory_report, factory_runner, factory_sessions as fs  # noqa: E402
from orch.core import ledger, permits, store  # noqa: E402
from orch.dashboard import launch  # noqa: E402

ASK = "Build the <b>export</b> & keep \"quotes\".\n\n### Detail\n- one line\n- `two` lines\n\nlast line"
DONE = "Exports open in the viewer."
CMD = "make deploy '<staging>'"  # one plain command (quoted), with text a page must escape
STATIC = Path(orch.__file__).parent / "dashboard" / "static"


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


def _once(c):
    return re.search(r'name="once" value="([^"]+)"', c.get("/new").text).group(1)


def _new(c, mode="dark", **over):
    data = {"title": "Export revamp", "mode": mode, "ask": ASK, "done_when": DONE, "size": "m", "priority": "normal",
            "once": _once(c)}
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


def _panel(html):
    return html.split('class="card ring-panel', 1)[1].split('"', 1)[0]


def _ring(html):
    return html.split('class="ring"', 1)[1].split("</svg>", 1)[0]


def _status(html):
    return html.split('id="run-status"', 1)[1].split("</h2>", 1)[0]


def _text(fragment):
    import html as h
    return " ".join(h.unescape(re.sub(r"<[^>]+>", " ", fragment)).split())


def _chip_head(html):
    """(chip words, headline) of the run view's status line, as text; the headline never repeats the chip."""
    status = html.split('id="run-status"', 1)[1].split("</h2>", 1)[0]
    chip = _text(status.split('class="chip', 1)[1].split(">", 1)[1].split("</span>", 1)[0])
    head = _text(status.split("</span>", 1)[1])
    assert chip.lower() not in head.lower() or chip == "Waiting" and head.startswith("Waiting for"), (chip, head)
    return chip, head


def _finish(c, ws, fa, close_tasks, eid, cid):
    fa.claim(cid)
    close_tasks(fa, cid)
    fa.set_section(cid, "Verification", "- AC1: ran it, green")
    fa.move(cid, "testing")
    seen = factory_report.ready(ws, _epic(ws, eid))["seen"]
    assert "err=" not in _loc(_post(c, f"/t/{eid}/verdict", verdict="done", seen=seen, next=f"/factory/{eid}"))


# -- the New ticket page: the mode choice --------------------------------------------------------------------------

def test_no_mode_choice_while_the_factory_is_off(dash):
    html = dash.get("/new").text
    assert 'name="mode"' not in html and "AI Factory" not in html and 'name="done_when"' not in html
    assert 'data-mode="ticket"' in html


def test_mode_choice_with_the_factory_on_and_dark_off(fws):
    html = _client(fws).get("/new").text
    assert 'id="mode-ticket"' in html and 'id="mode-factory"' in html and 'id="mode-dark"' not in html
    assert re.search(r'id="mode-ticket"[^>]*checked', html)
    assert "Dark AI Factory is off. Turn it on in a terminal with <code>orch factory dark on</code>." in html
    assert 'name="confirm_dark"' not in html
    assert "Everything in Requirements is done." in html  # Done when, prefilled
    assert "Creates an epic and starts it at once." in html and 'class="only-ticket">Describe the ask.' in html
    assert 'data-factory-confirm="up to 25 children or 72 hours, children of size m or smaller"' in html


def test_mode_choice_with_dark_on_says_what_reaches_you(dws):
    html = _client(dws).get("/new").text
    assert 'id="mode-dark"' in html and 'name="confirm_dark"' in html and "data-dark-off" not in html
    assert "Type <b>dark</b> to confirm: you sign a Dark charter, and its sessions show no permission prompts." in html
    assert "you still answer cards, larger children and the verdict" in html
    assert "with a button to add it to the profile" in html and "Nothing splits the epic" not in html
    assert "A planner session splits the epic into children" in html
    assert "The Dark profile must hold the orch commands it runs." in html
    for claim in ("Nothing asks you", "nothing asks you", "without any prompts", "Agents split it", "one button"):
        assert claim not in html


def test_the_page_shows_the_ticket_view_without_has_or_js():
    """The mode rules: a plain data-mode rule hides the factory fields by default (no :has() needed), and :has() rules
    follow the radios with JS off."""
    css = (STATIC / "app.css").read_text()
    assert ".new-ticket[data-mode=ticket] :is(.only-factory, .only-dark)" in css
    assert ".new-ticket:has(#mode-dark:checked) .only-dark { display: var(--shown); }" in css
    js = (STATIC / "app.js").read_text()
    assert "box.dataset.mode = mode" in js and "form[data-new-form]" in js


def test_the_confirm_dialog_only_fills_words_the_server_still_requires(dws):
    """With JS the confirm dialog (confirm.js) fills confirm_dark after the person confirms; without JS the same input
    is typed into, shown in a .nojs-only block. Either way the server requires the word: a post without it (a forged
    one, or a dialog that never ran) creates nothing."""
    c = _client(dws)
    html = c.get("/new").text
    form = html.split('<form class="card form-card" method="post" action="/new"', 1)[1].split("</form>", 1)[0]
    assert 'data-new-form data-confirm-build="start"' in form
    word = form.split('<label class="field-col only-dark nojs-only" for="confirm_dark">', 1)[1].split("</label>", 1)[0]
    assert re.search(r'<input id="confirm_dark" name="confirm_dark"[^>]*data-confirm-word="dark"', word)
    assert not re.search(r'name="confirm_(dark|production)"[^>]*value=', form)  # never pre-filled: typed, or filled on confirm
    before = len(list(dws.tickets_dir.rglob("*.md")))
    for forged in ({"confirm_dark": ""}, {"confirm_dark": "yes"}, {"confirm_dark": "", "release": "none"}):
        r = _new(c, "dark", **forged)
        assert r.status_code == 422 and "Type dark to start a Dark AI Factory" in r.text
    data = {"title": "x", "mode": "dark", "ask": ASK, "done_when": DONE, "once": _once(c)}  # no confirm_dark at all
    assert _post(c, "/new", **data).status_code == 422
    assert len(list(dws.tickets_dir.rglob("*.md"))) == before


def test_a_refused_dark_post_keeps_the_dark_view(dws):
    r = _new(_client(dws), "dark", confirm_dark="")
    assert r.status_code == 422 and 'data-mode="dark"' in r.text and re.search(r'id="mode-dark"[^>]*checked', r.text)


# -- the New ticket page: creating and starting ----------------------------------------------------------------------

def test_dark_start_from_the_new_page_creates_signs_and_arms(dws, human):
    c = _client(dws)
    r = _new(c, "dark", type="feature")  # the type is forced to epic
    assert r.status_code == 303 and "err=" not in _loc(r) and "started+it+as+a+Dark+AI+Factory" in _loc(r)
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
    r = _new(c, "factory")
    assert "started+it+as+an+AI+Factory" in _loc(r)
    eid = re.search(r"/factory/([A-Z]+-\d+)", _loc(r)).group(1)
    d = epics.delegation(fws, _epic(fws, eid))
    assert d["factory"] and not d.get("dark") and fs.armed(fws, d["id"])


def test_a_ticket_from_the_new_page_is_as_before(fws):
    c = _client(fws)
    r = _post(c, "/new", title="Plain one", mode="ticket", ask="just do it", done_when=DONE)  # no token needed
    assert "/t/" in _loc(r) and "err=" not in _loc(r)
    (e,) = store.scan(fws)
    t = store.read_ticket(e.path)
    assert t.meta["type"] == "feature" and t.status == "backlog" and t.section("Ask") == "just do it"
    assert not t.section("Acceptance criteria") and epics.delegation(fws, t) is None


@pytest.mark.parametrize("ws_kind, data, why", [
    ("dark", {"confirm_dark": ""}, "Type dark"),
    ("dark", {"confirm_dark": "Dark"}, "Type dark"),
    ("dark", {"confirm_dark": "yes"}, "Type dark"),
    ("factory", {}, "Dark AI Factory is off"),  # Dark off: a posted Dark mode is refused
    ("off", {}, "AI Factory is switched off"),
    ("dark", {"mode": "darker"}, "Unknown mode"),  # a forged mode
    ("off", {"mode": "factory"}, "AI Factory is switched off"),
    ("dark", {"ask": "  "}, "Describe the work"),
    ("dark", {"done_when": ""}, "Say when it is done"),
    ("dark", {"ask": "Build it.\nFormat: TBD"}, "reads as a question still open for you"),
    ("dark", {"done_when": "Open questions for you: which viewer?"}, "reads as a question still open for you"),
    ("dark", {"ask": "Build​ it."}, "hidden or control characters"),
    ("dark", {"title": "Export‮ revamp"}, "hidden or control characters"),
])
def test_a_start_that_would_be_refused_creates_nothing(configure, human, ws_kind, data, why):
    from orch.core.ops import Ops
    ws = configure(factory={"enabled": ws_kind != "off"})
    if ws_kind == "dark":
        Ops(ws, human).set_factory_dark(True)
    n = len(ledger.entries(ws))
    r = _new(_client(ws), **{"mode": "dark", **data})
    assert r.status_code == 422 and why in r.text
    assert store.scan(ws) == [] and len(ledger.entries(ws)) == n
    assert "`" not in r.text.split('role="alert"', 1)[1].split("</p>", 1)[0]  # plain words, no code marks


def test_a_second_submit_of_the_same_form_starts_nothing(dws):
    c = _client(dws)
    once = _once(c)
    data = {"title": "Export revamp", "mode": "dark", "ask": ASK, "done_when": DONE, "confirm_dark": "dark", "once": once}
    first = _post(c, "/new", **data)
    eid = re.search(r"/factory/([A-Z]+-\d+)", _loc(first)).group(1)
    second = _post(c, "/new", **data)
    assert second.status_code == 409 and f"This form was sent already: it created {eid}" in second.text
    assert f'href="/factory/{eid}"' in second.text and 'value="Export revamp"' not in second.text
    assert len(store.scan(dws)) == 1 and len([e for e in ledger.entries(dws) if e.get("kind") == "charter"]) == 1
    for token in ("", "made-up"):
        r = _post(c, "/new", **{**data, "once": token})
        assert r.status_code == 409 and "sent already or is too old" in r.text
    assert len(store.scan(dws)) == 1


def test_the_submit_button_is_disabled_after_the_first_factory_submit():
    js = (STATIC / "app.js").read_text()
    assert "form.dataset.sent" in js and "b.disabled = true" in js
    assert 'modeOf(form) === "ticket") return' in js  # Ticket mode posts as before
    # a string check (the behaviour itself is tests/js/factory_forms.js, run by test_inline_confirm_js.py)
    assert "PROFILE ONLY" not in js and "START DARK" not in js and "NO PROMPTS" not in js


def test_a_failed_start_after_creation_goes_to_the_epic_not_a_form_error(dws, monkeypatch):
    c = _client(dws)
    once = _once(c)
    monkeypatch.setenv("ORCH_HARNESS", "test-agent")  # the dashboard process runs under an agent: no start
    r = _post(c, "/new", title="x", mode="dark", ask=ASK, done_when=DONE, confirm_dark="dark", once=once)
    assert r.status_code == 303 and "err=" in _loc(r) and "/t/" in _loc(r)
    assert "but+starting+it+as+a+Dark+AI+Factory+failed" in _loc(r) and "agent+harness" in _loc(r)
    assert "%60" not in _loc(r)  # no backticks in the flash
    (e,) = store.scan(dws)
    assert epics.delegation(dws, store.read_ticket(e.path)) is None
    assert not [x for x in ledger.entries(dws) if x.get("kind") == "charter"]


def test_a_start_is_refused_when_the_stored_text_is_not_what_was_sent(dws, monkeypatch):
    from orch.core import ops as ops_mod
    real = ops_mod.Ops.new

    def edited(self, title, **kw):  # something rewrites the epic between creation and the start
        t = real(self, title, **kw)
        real_set = ops_mod.Ops.set_section
        real_set(self, t.id, "Requirements", "something else")
        return t
    monkeypatch.setattr(ops_mod.Ops, "new", edited)
    r = _new(_client(dws), "dark")
    assert "err=" in _loc(r) and "not+exactly+what+you+typed" in _loc(r)
    (e,) = store.scan(dws)
    assert epics.delegation(dws, store.read_ticket(e.path)) is None


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

def test_the_epic_page_offers_dark_only_while_dark_is_on_and_needs_the_word(fws, fa, human):
    e = fa.new("Epic", type="epic")
    _refine(fa, e.id, plan=None)
    page = _client(fws).get(f"/t/{e.id}").text
    assert 'name="start" value="factory"' in page and 'name="start" value="dark"' not in page and 'name="confirm_dark"' not in page
    seen = epics.charter(fws, _epic(fws, e.id))["content_hash"]
    r = _post(_client(fws), f"/t/{e.id}/approve", gate="requirements", seen=seen, dark="1", confirm_dark="dark")
    assert "err=" in _loc(r) and epics.delegation(fws, _epic(fws, e.id)) is None  # Dark off: refused
    from orch.core.ops import Ops
    Ops(fws, human).set_factory_dark(True)
    c = _client(fws)
    page = c.get(f"/t/{e.id}").text
    radios = re.findall(r'<input class="sr-only" type="radio" id="start-[^"]*" name="start" value="([^"]*)"', page)
    assert radios == ["", "factory", "dark"] and 'type="checkbox" name="dark"' not in page
    assert 'type="checkbox" name="factory"' not in page
    assert re.search(r'name="start" value=""\s+checked', page)  # None is the default
    field = page.split('class="field-col start-dark-only nojs-only"', 1)[1].split("</label>", 1)[0]
    # the typed word sits in the Dark-only field, shown without JS; with JS the dialog fills it in after you confirm
    assert 'name="confirm_dark"' in field and 'data-confirm-word="dark"' in field
    form = page.split('action="/t/' + e.id + '/approve" class="inline-confirm-form charter-form"', 1)[1].split("</form>", 1)[0]
    assert 'data-confirm-build="start"' in page and '<span class="lbl-dark">Start Dark AI Factory</span>' in form
    assert "its sessions show no permission prompts" in page and "you answer the commands they need on cards" in page
    for gone in ("nothing asks you", "without asking you", "no permission prompts in the session"):
        assert gone not in page
    css = (STATIC / "app.css").read_text()
    assert ".charter-factory .start-dark-only { display: none; }" in css
    assert ".charter-factory[data-start=dark] .start-dark-only," in css
    assert ".charter-factory:has(input[name=start][value=dark]:checked) .start-dark-only { display: flex; }" in css
    for word in ("", "Dark", "no"):
        r = _post(c, f"/t/{e.id}/approve", gate="requirements", seen=seen, dark="1", confirm_dark=word)
        assert "err=" in _loc(r) and "type+dark" in _loc(r) and epics.delegation(fws, _epic(fws, e.id)) is None
    r = _post(c, f"/t/{e.id}/approve", gate="requirements", seen=seen, dark="1", confirm_dark="dark")
    assert "err=" not in _loc(r)
    d = epics.delegation(fws, _epic(fws, e.id))
    assert d["factory"] and d["dark"] and fs.armed(fws, d["id"])


def test_the_epic_page_dark_start_is_refused_to_an_agent_harness_and_cross_origin(dws, fa, monkeypatch):
    e = fa.new("Epic", type="epic")
    _refine(fa, e.id, plan=None)
    seen = epics.charter(dws, _epic(dws, e.id))["content_hash"]
    c = _client(dws)
    data = {"gate": "requirements", "seen": seen, "dark": "1", "confirm_dark": "dark"}
    assert c.post(f"/t/{e.id}/approve", data=data, headers={"origin": "http://evil.example"},
                  follow_redirects=False).status_code == 403
    monkeypatch.setenv("ORCH_HARNESS", "test-agent")
    r = _post(c, f"/t/{e.id}/approve", **data)
    assert "err=" in _loc(r) and "agent+harness" in _loc(r) and epics.delegation(dws, _epic(dws, e.id)) is None


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
    pay = lambda cmd: {"session_id": b["session"], "tool_name": "Bash", "tool_input": {"command": cmd},
                   "cwd": b["start"]}  # noqa: E731
    assert _behavior(permits.hook_decision(dws, pay("make test --quiet"))) == "allow"  # the profile covers it
    out = permits.hook_decision(dws, pay("make lint"))
    assert _behavior(out) == "deny" and "not in the Dark profile" in out["hookSpecificOutput"]["decision"]["message"]
    (r,) = permits.open_requests(dws)
    assert r["source"] == "dark" and r["command"] == "make lint"
    for url in ("/", f"/factory/{eid}"):
        card = c.get(url).text.split(f'data-permit="{r["id"]}"', 1)[1].split("</article>", 1)[0]
        assert f'action="/permits/{r["id"]}/profile"' in card and "not in the Dark profile" in card
        assert '<button type="submit" class="btn">Add to the Dark profile</button>' in card  # not the primary
        assert '<button type="submit" class="btn btn-primary">Grant once</button>' in card and "Deny" in card
        assert "this exact command runs from now on in Dark epics of this checkout" in card
    resp = _post(c, f"/permits/{r['id']}/profile", sha=r["sha"], next=f"/factory/{eid}")
    assert "err=" not in _loc(resp) and _loc(resp).startswith(f"/factory/{eid}")
    assert any(x["kind"] == "exact" and x["rule"] == "make lint" for x in dark_profile.rules(dws))
    assert permits.open_requests(dws) == [] and f'data-permit="{r["id"]}"' not in c.get(f"/factory/{eid}").text
    assert _behavior(permits.hook_decision(dws, pay("make lint"))) == "allow"
    assert b["session"] not in c.get(f"/factory/{eid}").text  # session ids never reach a page


# -- POST /permits/{rid}/profile and the Add button --------------------------------------------------------------------

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
    card = c.get("/").text.split(f'data-permit="{r["id"]}"', 1)[1].split("</article>", 1)[0]
    assert "Add to the Dark profile" not in card


def test_profile_post_refuses_another_workspaces_request(dws, dark_request):
    c, eid, cid, r = dark_request
    body = permits._dir("requests") / f"{r['id']}.json"
    data = json.loads(body.read_text())
    body.write_text(json.dumps({**data, "workspace": "someone-else"}))  # filed by another workspace
    n = len(ledger.entries(dws))
    resp = _post(c, f"/permits/{r['id']}/profile", sha=r["sha"])
    assert "err=" in _loc(resp) and len(ledger.entries(dws)) == n and dark_profile.rules(dws) == []


def _agent():
    from orch.core.events import Actor
    return Actor("agent", "claude-code", "cli", "7f3c9a21-0000")


def test_profile_post_is_the_humans_only(dws, dark_request, monkeypatch):
    c, eid, cid, r = dark_request
    from orch.dashboard.app import create_app
    app = create_app(dws, "tok")
    assert TestClient(app).post(f"/permits/{r['id']}/profile", data={"sha": r["sha"]},
                                follow_redirects=False).status_code == 401
    other = TestClient(app)
    other.get("/?token=tok")
    assert other.post(f"/permits/{r['id']}/profile", data={"sha": r["sha"]}, headers={"origin": "http://evil.example"},
                      follow_redirects=False).status_code == 403
    assert dark_profile.rules(dws) == []
    assert _post(c, f"/permits/{r['id']}/profile", sha=r["sha"]).status_code == 303
    assert dark_profile.rules(dws)  # the human's own post works ...
    r2 = permits.request(dws, _agent(), _epic(dws, cid), "make two", source="dark")
    monkeypatch.setenv("ORCH_HARNESS", "test-agent")
    n = len(ledger.entries(dws))
    resp = _post(c, f"/permits/{r2['id']}/profile", sha=r2["sha"])
    assert "err=" in _loc(resp) and "agent+harness" in _loc(resp) and len(ledger.entries(dws)) == n


def test_the_add_button_shows_only_while_dark_is_on(dws, human, dark_request):
    from orch.core.ops import Ops
    c, eid, cid, r = dark_request
    assert "Add to the Dark profile" in c.get("/").text
    Ops(dws, human).set_factory_dark(False)
    card = c.get("/").text.split(f'data-permit="{r["id"]}"', 1)[1].split("</article>", 1)[0]
    assert "Add to the Dark profile" not in card and "Grant once" in card


def test_a_compound_card_offers_no_profile_button_and_says_why(dws, fa, dark_request):
    c, eid, cid, plain = dark_request
    for cmd in ("make a && make b", "orch wait L-1 2>&1 | head -20", "make test > out.txt",
                "orch log L-1 -m \"$(id)\""):
        r = permits.request(dws, fa.actor, _epic(dws, cid), cmd, source="dark")
        card = c.get("/").text.split(f'data-permit="{r["id"]}"', 1)[1].split("</article>", 1)[0]
        assert "Add to the Dark profile" not in card and "data-compound" in card, cmd
        assert "adding it would not help. Grant once or Deny." in card
        assert '<button type="submit" class="btn btn-primary">Grant once</button>' in card
    card = c.get("/").text.split(f'data-permit="{plain["id"]}"', 1)[1].split("</article>", 1)[0]
    assert "Add to the Dark profile" in card and "data-compound" not in card  # a plain command keeps the button


def test_a_card_from_an_ordinary_factory_has_no_profile_button(fws, fa, fh):
    e = fa.new("Epic", type="epic")
    _refine(fa, e.id, plan=None)
    fh.approve(e.id, "requirements", delegate={"factory": True})
    cid = _child(fa, e.id)
    r = permits.request(fws, fa.actor, _epic(fws, cid), CMD)
    card = _client(fws).get("/").text.split(f'data-permit="{r["id"]}"', 1)[1].split("</article>", 1)[0]
    assert "Add to the Dark profile" not in card and "Grant once" in card


# -- the run view -----------------------------------------------------------------------------------------------------

def test_run_view_without_children_waits_for_them(dws):
    c = _client(dws)
    eid = _started(c, dws, "dark")
    html = c.get(f"/factory/{eid}").text
    assert "Waiting for children" in _status(html) and "chip-neu" in _status(html) and "is working" not in html
    assert "The runner starts a planner session that splits the epic into children when a session slot is free" in html
    assert 'aria-valuetext="Step 1 of 5: Understand, not yet"' in html  # Understand needs a child
    assert "is-live" not in _panel(html) and "hot" not in _panel(html)


def test_run_view_while_the_planner_splits_the_epic(dws, human):
    c = _client(dws)
    eid = _started(c, dws, "dark")
    fake = Fake()
    _tick(dws, human, fake)  # the runner starts the planner
    html = c.get(f"/factory/{eid}").text
    assert _chip_head(html) == ("Planning", "A planner session is splitting the epic into children")
    assert "chip-ok" in _status(html) and "Waiting for children" not in html  # Dark and working: the mint chip
    assert 'aria-valuetext="Step 1 of 5: Understand, in progress"' in html  # still no child: Understand not lit
    assert "is-live" in _panel(html) and "hot" not in _panel(html) and "data-planning" in html
    assert "1 factory, 1 working" in c.get("/factory").text
    fake.names.clear()  # it ended without a child
    _tick(dws, human, fake)
    html = c.get(f"/factory/{eid}").text
    assert _chip_head(html) == ("No children", "Waiting for children")
    assert "The planner session ended without adding a child, and no card of this epic is open." in html
    assert "your answer to one of its cards" not in html
    d = epics.delegation(dws, _epic(dws, eid))
    fs.mark_planner_run(dws, d["id"])
    html = c.get(f"/factory/{eid}").text
    assert "The planner session started twice and ended without adding a child" in html


def test_an_empty_dark_profile_is_said_on_the_run_view_and_the_new_page(dws, human):
    c = _client(dws)
    eid = _started(c, dws, "dark")
    note = "The Dark profile is empty, so each command will stop for a card. Add the baselines in a terminal: " \
           "<code>orch dark profile add --baseline</code> (orch's agent verbs) and " \
           "<code>orch dark profile add --baseline git-basic</code> (so workers can commit)"
    assert note in c.get(f"/factory/{eid}").text and note in c.get("/new").text
    plain = _started(c, dws, "factory")
    assert "data-profile-empty" not in c.get(f"/factory/{plain}").text  # not a Dark run
    dark_profile.add_baseline(dws, human)
    assert "data-profile-empty" not in c.get(f"/factory/{eid}").text and "data-profile-empty" not in c.get("/new").text


def test_run_view_working_dark_and_the_glow(dws, fa, human, close_tasks):
    c = _client(dws)
    eid = _started(c, dws, "dark")
    cid = _child(fa, eid)
    html = c.get(f"/factory/{eid}").text
    assert _chip_head(html) == ("Waiting", "A child's session starts in the runner's next round")  # launchable, no session yet
    assert "data-slot" in html and "one of its 3 session slots is free" in html and "is-live" not in _panel(html)
    _tick(dws, human, Fake())  # a session runs on the child now
    html = c.get(f"/factory/{eid}").text
    assert _chip_head(html) == ("Working", "Sessions are running on its children") and "chip-ok" in _status(html)
    assert 'role="status"' in html and 'role="progressbar"' in html
    assert 'aria-valuenow="2"' in html and 'aria-valuetext="Step 3 of 5: Build, in progress"' in html
    assert _panel(html).strip() == "is-dark is-live"  # a live session is no build evidence: the faint glow only
    ring = _ring(html)
    assert ring.count("is-done") == 2 and ring.count("is-now") == 1 and "ring-run" not in ring and "ring-core" in ring
    assert "No estimate yet. Running for" in html and "Stop the run…" in html and "Started " not in html
    assert "Build is as the agents report it" in html
    steps = re.findall(r'class="step-mark is-[a-z]+"[^>]*>\s*<[^>]+>\s*([A-Za-z]+)', html) or \
        re.findall(r">(Understand|Plan|Build|Evidence|Done|Test|Review|Merge|Release)<", html)
    assert not {"Test", "Review", "Merge", "Release"} & set(steps)
    fa.claim(cid)
    close_tasks(fa, cid)  # a task closed: real build evidence
    assert "hot" in _panel(c.get(f"/factory/{eid}").text)


def test_run_view_working_ai_factory_moves_its_dash(fws, fa, human):
    c = _client(fws)
    eid = _started(c, fws, "factory")
    _child(fa, eid)
    _tick(fws, human, Fake())
    html = c.get(f"/factory/{eid}").text
    assert _chip_head(html) == ("Working", "Sessions are running on its children") and "chip-info" in _status(html)
    assert "is-dark" not in html and "is-live" in _panel(html)
    assert '<path class="ring-run"' in _ring(html) and "ring-core" not in html
    css = (STATIC / "app.css").read_text()
    assert "stroke-dasharray: 0.4 1.6" in css and "to { stroke-dashoffset: -2; }" in css  # one period: no snap


def test_run_view_no_motion_unless_working(dws, fa):
    c = _client(dws)
    eid = _started(c, dws, "dark")
    _child(fa, eid)
    _post(c, f"/t/{eid}/epic/pause", next=f"/factory/{eid}")
    html = c.get(f"/factory/{eid}").text
    assert "is-live" not in _panel(html) and "hot" not in _panel(html) and "ring-run" not in html
    css = (STATIC / "app.css").read_text()
    assert ".is-live .ring-core { animation" in css and ".ring-core { fill: var(--mint); opacity: 0.06; }" in css


def test_run_view_with_the_dark_switch_off_is_an_ai_factory(dws, fa, human):
    from orch.core.ops import Ops
    c = _client(dws)
    eid = _started(c, dws, "dark")
    _child(fa, eid)
    Ops(dws, human).set_factory_dark(False)
    _tick(dws, human, Fake())
    html = c.get(f"/factory/{eid}").text
    assert "· AI Factory</p>" in html and "· Dark AI Factory</p>" not in html and "chip-info" in _status(html)
    assert "is-dark" not in html and "ring-core" not in html and '<path class="ring-run"' in html
    assert "Dark switch is off" in html and "Commands outside your grants and the harness's allowlist are cards" in html
    assert "every command it needs is a card" not in html
    row = c.get("/factory").text
    assert "AI Factory (Dark switch off)" in row


def test_run_view_waiting_for_you_shows_the_card(dws, fa, dark_request):
    c, eid, cid, r = dark_request
    html = c.get(f"/factory/{eid}").text
    assert _chip_head(html) == ("Needs you", "Your answer is needed on the cards below")
    assert re.search(r'<h2 id="run-waiting-h">Your cards</h2>', html)
    assert f'data-permit="{r["id"]}"' in html and "is-wait" in _ring(html)
    assert f'name="next" value="/factory/{eid}"' in html


def test_run_view_paused_after_stop_the_run(dws, fa):
    c = _client(dws)
    eid = _started(c, dws, "dark")
    _child(fa, eid)
    resp = _post(c, f"/t/{eid}/epic/pause", next=f"/factory/{eid}")
    assert "err=" not in _loc(resp) and _loc(resp).startswith(f"/factory/{eid}")
    html = c.get(f"/factory/{eid}").text
    assert _chip_head(html) == ("Paused", "You stopped the run")
    assert "is-stop" in _ring(html) and "Stop the run…" not in html
    assert re.search(r'data-time>Started [^<]+ ago\.</p>', html) and "No estimate yet." not in html
    assert "Running for" not in html


def test_run_view_stopped_and_budget_used_up(dws, fa, human, dark_request, monkeypatch):
    from orch import clock
    c, eid, cid, r = dark_request
    permits.permit_deny(dws, human, r["id"], expected_sha=r["sha"])
    html = c.get(f"/factory/{eid}").text
    assert "data-stopped=" in html and "Permission denied" in html and "is-stop" in _ring(html)
    assert _chip_head(html) == ("Stopped", "The agents cannot go on by themselves") and "chip-warn" in _status(html)
    real = clock.now()
    monkeypatch.setattr(clock, "now", lambda: real + timedelta(hours=73))
    html = c.get(f"/factory/{eid}").text
    assert "Stopped" in _status(html) and "time budget of 72 hours used up" in html


def test_run_view_expired_time_budget(dws, fa, monkeypatch):
    from orch import clock
    c = _client(dws)
    eid = _started(c, dws, "dark")
    _child(fa, eid)
    real = clock.now()
    monkeypatch.setattr(clock, "now", lambda: real + timedelta(hours=73))
    html = c.get(f"/factory/{eid}").text
    assert _chip_head(html) == ("Budget used up", "Agents stopped on this epic")
    assert "is-live" not in _panel(html) and "time budget of 72 hours used up" in html


def test_run_view_runner_blocked_and_unarmed(dws, fa, fh, monkeypatch):
    c = _client(dws)
    eid = _started(c, dws, "dark")
    _child(fa, eid)
    monkeypatch.setattr(factory_runner, "user_settings_blocker", lambda environ=None: "the orch hooks are missing")
    html = c.get(f"/factory/{eid}").text
    assert _chip_head(html) == ("Blocked", "The runner starts nothing")
    assert "the orch hooks are missing" in html
    e2 = fa.new("Terminal start", type="epic")
    _refine(fa, e2.id, plan=None)
    fh.approve(e2.id, "requirements", delegate={"factory": True})  # the terminal's start arms nothing
    html = c.get(f"/factory/{e2.id}").text
    assert _chip_head(html) == ("Not running", "The dashboard's start did not arm it (for example, it was "
                                                "approved in a terminal)")


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
    assert _chip_head(html) == ("Finished", "You gave the verdict")
    assert 'aria-valuenow="5"' in html and _ring(html).count("is-done") == 5
    summary = html.split('id="run-summary-h"', 1)[1].split("</section>", 1)[0]
    assert "1 child, 1 task done, as the agents report it" in summary
    assert "0 permission requests: 0 answered on a card, 0 added to the Dark profile" in summary
    assert "Commands the Dark profile allowed directly leave no record and are not counted." in summary
    assert re.search(r"<li>[A-Z][^<]* from your start to your verdict</li>", summary)
    assert "Stop the run…" not in html and "data-time" not in html and "No estimate yet." not in html
    assert html.count("from your start to your verdict") == 1  # the duration, said once


def test_the_summary_counts_cards_added_to_the_profile(dws, fa, close_tasks, dark_request):
    c, eid, cid, r = dark_request
    assert "err=" not in _loc(_post(c, f"/permits/{r['id']}/profile", sha=r["sha"]))
    r2 = permits.request(dws, fa.actor, _epic(dws, cid), "make lint", source="dark")
    _post(c, f"/permits/{r2['id']}/grant", sha=r2["sha"], scope="once")
    _finish(c, dws, fa, close_tasks, eid, cid)
    summary = c.get(f"/factory/{eid}").text.split('id="run-summary-h"', 1)[1].split("</section>", 1)[0]
    assert "2 permission requests: 1 answered on a card, 1 added to the Dark profile" in summary


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


def test_run_view_escapes_the_title_and_the_log_is_plain_words(dws, fa, dark_request):
    c, eid, cid, r = dark_request
    path, t = store.load(dws, eid)
    t.meta["title"] = "<script>alert(1)</script>"
    store.save(dws, t, path)
    html = c.get(f"/factory/{eid}").text
    assert "<script>alert(1)</script>" not in html and "&lt;script&gt;alert(1)&lt;/script&gt;" in html
    log = html.split('<details class="run-log">', 1)[1]
    assert "<summary>Show the log</summary>" in log and "Who wrote each entry is as the agents report it." in log
    assert "An agent asked for a permission" in log and "You approved a gate" in log
    assert "permit.requested" not in log and "make" not in log and "deploy" not in log and r["sha"][7:] not in log
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

def test_factory_list_order_counts_and_menu(dws, fa, human, close_tasks, monkeypatch):
    c = _client(dws)
    working = _started(c, dws, "dark")
    _child(fa, working)
    _tick(dws, human, Fake())  # a session runs on its child
    waiting = _started(c, dws, "factory")
    w_child = _child(fa, waiting)
    permits.request(dws, fa.actor, _epic(dws, w_child), CMD)
    paused = _started(c, dws, "factory")
    _child(fa, paused)
    _post(c, f"/t/{paused}/epic/pause")
    finished = _started(c, dws, "factory")
    _finish(c, dws, fa, close_tasks, finished, _child(fa, finished))
    calls = []
    monkeypatch.setattr(factory_runner, "user_settings_blocker", lambda environ=None: calls.append(1))
    html = c.get("/factory").text
    assert len(calls) == 1  # read once for the whole list
    assert "4 factories, 1 working, 1 needs you" in html
    assert re.findall(r'data-factory="([A-Z]+-\d+)"', html) == [waiting, working, paused, finished]
    row = html.split(f'data-factory="{working}"', 1)[1].split("</li>\n", 1)[0]
    assert ">Dark AI Factory<" in row and "chip-ok" in row and row.count('class="step-mark is-') == 5
    assert 'href="/factory"' in html and ">Factories<" in html


def test_factory_list_counts_stopped_as_needing_you(dws, fa, human, dark_request):
    c, eid, cid, r = dark_request
    permits.permit_deny(dws, human, r["id"], expected_sha=r["sha"])
    assert "1 factory, 0 working, 1 needs you" in c.get("/factory").text


def test_factory_list_empty_state_and_no_menu_entry_while_off(fws, configure):
    html = _client(fws).get("/factory").text
    assert "No factories yet" in html and "0 factories, 0 working, 0 need you" in html
    assert 'href="/factory"' not in _client(configure(factory={"enabled": False})).get("/").text


# -- the New ticket page, as the browser would show it per mode --------------------------------------------------------

def _hidden_by_mode(css):
    """{mode: classes hidden in it}, from the plain data-mode rules of app.css (the no-:has() path)."""
    out = {}
    for mode, classes in re.findall(r"\.new-ticket\[data-mode=(\w+)\] :is\(([^)]*)\)", css):
        out[mode] = {c.strip().lstrip(".") for c in classes.split(",")}
    return out


def _elements(html):
    """Every element as (its own classes, the classes of it and its ancestors, its text with descendants)."""
    from html.parser import HTMLParser
    void = {"input", "br", "img", "meta", "link", "hr", "source", "use"}

    class P(HTMLParser):
        def __init__(self):
            super().__init__()
            self.stack, self.done = [], []

        def handle_starttag(self, tag, attrs):
            cls = set((dict(attrs).get("class") or "").split())
            el = {"tag": tag, "cls": cls, "all": cls | (self.stack[-1]["all"] if self.stack else set()), "text": "",
                  "attrs": dict(attrs)}
            if tag in void:
                self.done.append(el)
            else:
                self.stack.append(el)

        def handle_endtag(self, tag):
            while self.stack:
                el = self.stack.pop()
                self.done.append(el)
                if el["tag"] == tag:
                    break

        def handle_data(self, data):
            for el in self.stack:
                el["text"] += data

    p = P()
    p.feed(html)
    return p.done


def _shown(html, css, mode, needle):
    """Whether the element whose own text holds `needle` (the innermost one) is shown in `mode`."""
    hidden = _hidden_by_mode(css)[mode]
    els = [e for e in _elements(html) if needle in " ".join(e["text"].split())]
    assert els, needle
    el = min(els, key=lambda e: len(e["text"]))
    return not (el["all"] & hidden)


def test_the_new_page_shows_each_modes_fields_only(dws):
    html = _client(dws).get("/new").text
    css = (STATIC / "app.css").read_text()
    expect = {  # needle: the modes it is shown in
        "Create ticket": {"ticket"}, "Start AI Factory": {"factory"},
        "Start Dark AI Factory": {"dark"}, "Type dark to confirm": {"dark"},
        "becomes the epic's Acceptance criteria": {"factory", "dark"},
        "Creates an epic and starts it at once.": {"factory", "dark"},
        "An agent turns the ask into requirements": {"ticket"},
        "The Dark profile must hold the orch commands it runs.": {"dark"},
        "A permission card appears when an agent needs a command": {"factory"},
    }
    for needle, modes in expect.items():
        for mode in ("ticket", "factory", "dark"):
            assert _shown(html, css, mode, needle) == (mode in modes), (needle, mode)
    # the :has() path names the same split (the radios decide with JS off): checked from its rule text
    assert ".new-ticket:has(#mode-dark:checked) .only-dark { display: var(--shown); }" in css
    assert ".new-ticket:has(#mode-factory:checked) :is(.only-ticket, .only-dark)," in css


def test_the_empty_profile_note_sits_in_the_dark_field(dws):
    html = _client(dws).get("/new").text
    # next to the Dark fields and outside the no-JS typed word, so it shows with JS too (the dialog asks the word)
    word = html.split('<label class="field-col only-dark nojs-only" for="confirm_dark">', 1)[1].split("</label>", 1)
    assert 'data-confirm-word="dark"' in word[0] and "data-profile-empty" not in word[0]
    assert word[1].lstrip().startswith('<p class="field-col only-dark"><span class="muted" data-profile-empty')


# -- the runner's sessions cannot write files under a prompting permission mode ----------------------------------------

def _user_settings(monkeypatch, tmp_path, data):
    d = tmp_path / "claude-user"
    d.mkdir(exist_ok=True)
    (d / "settings.json").write_text(json.dumps(data), encoding="utf-8")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(d))


def test_edits_blocked_reads_the_user_permission_mode(monkeypatch, tmp_path):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "none"))
    assert factory_runner.edits_blocked()  # no settings: edits prompt
    for mode, blocked in (("default", True), ("plan", True), ("acceptEdits", False), ("auto", False),
                          ("bypassPermissions", True), (None, True)):  # bypass: no request reaches the hook
        _user_settings(monkeypatch, tmp_path, {"permissions": {"defaultMode": mode}} if mode else {})
        assert factory_runner.edits_blocked() is blocked, mode
    (tmp_path / "claude-user" / "settings.json").write_text("{", encoding="utf-8")
    assert factory_runner.edits_blocked()


def test_the_edit_mode_notice_on_the_run_view_and_the_new_page(dws, monkeypatch, tmp_path):
    note = ("Sessions started by the runner cannot write files unless your Claude permission mode allows edits. Set "
            "permissions.defaultMode to acceptEdits in your user settings, or the planner cannot create children.")
    _user_settings(monkeypatch, tmp_path, {"permissions": {"defaultMode": "default"}})
    c = _client(dws)
    eid = _started(c, dws, "dark")
    assert note in c.get(f"/factory/{eid}").text
    new = c.get("/new").text
    assert note in new and re.search(r'<p class="muted only-factory" data-edits-off>', new)
    before = (dws.root / "orchestrator" / "config.json").read_bytes()
    _user_settings(monkeypatch, tmp_path, {"permissions": {"defaultMode": "acceptEdits"}})
    assert note not in c.get(f"/factory/{eid}").text and note not in c.get("/new").text
    assert json.loads((tmp_path / "claude-user" / "settings.json").read_text()) == {
        "permissions": {"defaultMode": "acceptEdits"}}  # read only: never written
    assert (dws.root / "orchestrator" / "config.json").read_bytes() == before


def test_a_parked_planner_with_an_open_card_shows_the_card_not_the_parked_text(dws, human):
    c = _client(dws)
    eid = _started(c, dws, "dark")
    fake = Fake()
    _tick(dws, human, fake)
    (b,) = fs.bindings(dws)
    permits.hook_decision(dws, {"session_id": b["session"], "tool_name": "Bash", "tool_input": {"command": "make x"},
                                "cwd": b["start"]})
    fake.names.clear()
    _tick(dws, human, fake)
    html = c.get(f"/factory/{eid}").text
    assert _chip_head(html) == ("Needs you", "Your answer is needed on the cards below") and "data-nokids" not in html


# -- the epic page's Start radios ------------------------------------------------------------------------------------

def test_the_epic_start_radio_decides_what_is_signed(dws, fa):
    c = _client(dws)
    for start, extra, factory, dark in (("factory", {"dark": "1", "confirm_dark": "dark"}, True, False),
                                        ("dark", {"confirm_dark": "dark"}, True, True)):
        e = fa.new(f"Epic {start}", type="epic")
        _refine(fa, e.id, plan=None)
        seen = epics.charter(dws, _epic(dws, e.id))["content_hash"]
        if start == "dark":
            r = _post(c, f"/t/{e.id}/approve", gate="requirements", seen=seen, start="dark", confirm_dark="")
            assert "type+dark" in _loc(r) and epics.delegation(dws, _epic(dws, e.id)) is None
        r = _post(c, f"/t/{e.id}/approve", gate="requirements", seen=seen, start=start, **extra)
        assert "err=" not in _loc(r), _loc(r)
        d = epics.delegation(dws, _epic(dws, e.id))
        assert bool(d.get("factory")) is factory and bool(d.get("dark")) is dark and fs.armed(dws, d["id"])
    e = fa.new("Epic none", type="epic")
    _refine(fa, e.id, plan=None)
    seen = epics.charter(dws, _epic(dws, e.id))["content_hash"]
    assert "err=" not in _loc(_post(c, f"/t/{e.id}/approve", gate="requirements", seen=seen, start=""))
    assert not (epics.delegation(dws, _epic(dws, e.id)) or {}).get("factory")


# -- a Dark card: Grant once first ----------------------------------------------------------------------------------

def test_add_to_the_profile_comes_after_grant_once(dws, dark_request):
    c, eid, cid, r = dark_request
    card = c.get("/").text.split(f'data-permit="{r["id"]}"', 1)[1].split("</article>", 1)[0]
    labels = re.findall(r'<button type="submit" class="btn[^"]*">([^<]+)</button>', card)
    assert labels.index("Grant once") < labels.index("Add to the Dark profile")
