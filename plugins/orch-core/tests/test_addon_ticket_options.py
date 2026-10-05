"""Addon ticket options (manifest `ticket_options`): a yes/no choice an addon adds to every ticket. Core draws it on the
new-ticket form, the approve card and the ticket page, keeps it per ticket in the addon's state and lets only a human
set it."""
import json

import pytest

pytest.importorskip("fastapi")

from addon_fixtures import GOOD  # noqa: E402

FACTORY = '''
class Addon:
    def __init__(self, ctx):
        self.ctx = ctx
        self.providers = []
        self.events = []

    def on_event(self, event, outbox):
        self.events.append((event.kind, event.ticket, event.via))

    def widgets(self, slot, view):
        return []


def create(ctx):
    return Addon(ctx)
'''
OPTION = {"id": "notify", "label": "Notify my phone about this ticket", "help": "Off: it still syncs.", "default": False}


def _manifest(**over):
    m = {**GOOD, "capabilities": ["panel", "events"], "slots": ["guide.section"], "settings_schema": [],
         "ticket_options": [OPTION], **over}
    m.pop("menu", None)
    return m


@pytest.fixture
def option_addon(tmp_path, monkeypatch):
    from orch.addons import discovery, userfiles
    from orch.dashboard.launch import config_dir
    monkeypatch.setattr(discovery, "default_addons_dir", lambda: tmp_path / "no-defaults")
    folder = config_dir() / "addons" / "hello-status"
    (folder / "hello_status").mkdir(parents=True)
    (folder / "orch-addon.json").write_text(json.dumps(_manifest()), encoding="utf-8")
    (folder / "hello_status" / "__init__.py").write_text(FACTORY, encoding="utf-8")
    userfiles.record_install("hello-status", source={"kind": "path", "path": str(tmp_path)}, version="0.1.0",
                             requires_api="2", folder=folder)
    return folder


def _enable(ws, folder, trust=True, enable=True):
    from orch.addons import userfiles
    from orch.addons.manifest import load_manifest
    if trust:
        userfiles.record_trust("hello-status", folder, load_manifest(folder))
    if enable:
        userfiles.set_enabled(ws.root, "hello-status", True)


def _client(ws):
    from fastapi.testclient import TestClient
    from orch.addons.loader import AddonRegistry
    from orch.dashboard.app import create_app
    ws._addons = AddonRegistry.load(ws)
    c = TestClient(create_app(ws, "tok"))
    assert c.get("/?token=tok").status_code == 200
    return c


@pytest.fixture
def live(ws, option_addon):
    _enable(ws, option_addon)
    return _client(ws)


def _opt_path(ws):
    from orch.addons.ticket_options import path_of
    return path_of(ws, "hello-status")


# --- manifest ----------------------------------------------------------------------------------------------------

def test_manifest_accepts_a_boolean_ticket_option():
    from orch.addons.manifest import parse_manifest
    m = parse_manifest(_manifest())
    o = m.ticket_option("notify")
    assert (o.label, o.help, o.default) == (OPTION["label"], OPTION["help"], False)


@pytest.mark.parametrize("bad", [
    [{"id": "Notify", "label": "x"}], [{"id": "n", "label": ""}], [{"id": "n", "label": "x", "default": "yes"}],
    [{"id": "n", "label": "x", "extra": 1}], [{"id": "n", "label": "x"}, {"id": "n", "label": "y"}],
    [{"id": f"o{i}", "label": "x"} for i in range(4)], "notify",
])
def test_manifest_refuses_a_malformed_ticket_option(bad):
    from orch.addons.manifest import manifest_problems
    assert any("ticket_options" in p for p in manifest_problems(_manifest(ticket_options=bad)))


# --- render only for enabled + trusted addons --------------------------------------------------------------------

def test_new_form_ticket_page_and_approve_card_show_the_option(live, ws, put):
    tid = put("backlog", sections={"Requirements": "r", "Acceptance criteria": "- [ ] a"})
    assert OPTION["label"] in live.get("/new").text
    page = live.get(f"/t/{tid}").text
    assert OPTION["label"] in page and 'id="ticket-options"' in page and "Turn on" in page
    form = page.split('action="/t/' + tid + '/approve"')[1].split("</form>")[0]
    assert 'name="option_on" value="hello-status/notify"' in form and 'name="option_offered"' in form
    assert OPTION["label"] in live.get("/groom").text  # the decision card


@pytest.mark.parametrize("trust,enable", [(False, True), (True, False), (False, False)])
def test_nothing_renders_unless_enabled_and_trusted(ws, option_addon, put, trust, enable):
    _enable(ws, option_addon, trust=trust, enable=enable)
    c = _client(ws)
    tid = put("backlog")
    assert OPTION["label"] not in c.get("/new").text and OPTION["label"] not in c.get(f"/t/{tid}").text
    r = c.post(f"/t/{tid}/option", data={"option": "hello-status/notify", "value": "1"}, follow_redirects=False)
    assert not _opt_path(ws).exists()  # an addon that is off cannot be written to
    assert r.status_code in (303, 302, 200)


# --- values persisted, default off -------------------------------------------------------------------------------

def test_default_is_off_and_the_ticket_page_toggle_persists_and_announces(live, ws, put):
    tid = put("backlog")
    la = ws.addons.get("hello-status")
    assert la.ctx.ticket_option(tid, "notify") is False
    r = live.post(f"/t/{tid}/option", data={"option": "hello-status/notify", "value": "1"}, follow_redirects=False)
    assert r.status_code == 303
    assert la.ctx.ticket_option(tid, "notify") is True
    stored = json.loads(_opt_path(ws).read_text())
    assert stored == {tid: {"notify": True}}
    assert "Turn off" in live.get(f"/t/{tid}").text
    from orch.core.events import read_events
    ev = [e for e in read_events(ws, tid) if e.kind == "ticket.option"]
    assert len(ev) == 1 and ev[0].data == {"addon": "hello-status", "option": "notify", "value": True}
    live.post(f"/t/{tid}/option", data={"option": "hello-status/notify", "value": ""})
    assert la.ctx.ticket_option(tid, "notify") is False


def test_new_ticket_form_sets_the_option_when_ticked(live, ws):
    r = live.post("/new", data={"title": "Pushy", "option_offered": "hello-status/notify",
                                "option_on": "hello-status/notify"}, follow_redirects=False)
    assert r.status_code == 303
    tid = r.headers["location"].split("/t/")[1].split("?")[0]
    assert ws.addons.get("hello-status").ctx.ticket_option(tid, "notify") is True
    r = live.post("/new", data={"title": "Quiet", "option_offered": "hello-status/notify"}, follow_redirects=False)
    tid2 = r.headers["location"].split("/t/")[1].split("?")[0]
    assert ws.addons.get("hello-status").ctx.ticket_option(tid2, "notify") is False


def test_approve_applies_the_ticked_option_and_a_refused_approve_changes_nothing(live, ws, put):
    from conftest import seen_hash
    tid = put("backlog", sections={"Requirements": "r", "Acceptance criteria": "- [ ] a"})
    la = ws.addons.get("hello-status")
    live.post(f"/t/{tid}/approve", data={"gate": "requirements", "seen": "sha256:wrong", "option_offered": "hello-status/notify",
                                          "option_on": "hello-status/notify"})
    assert la.ctx.ticket_option(tid, "notify") is False
    live.post(f"/t/{tid}/approve", data={"gate": "requirements", "seen": seen_hash(ws, "approve", tid, "requirements"),
                                          "option_offered": "hello-status/notify", "option_on": "hello-status/notify"})
    assert la.ctx.ticket_option(tid, "notify") is True


def test_approve_without_the_option_in_the_form_leaves_it_alone(live, ws, put):
    from conftest import seen_hash
    tid = put("backlog", sections={"Requirements": "r", "Acceptance criteria": "- [ ] a"})
    la = ws.addons.get("hello-status")
    live.post(f"/t/{tid}/option", data={"option": "hello-status/notify", "value": "1"})
    live.post(f"/t/{tid}/approve", data={"gate": "requirements", "seen": seen_hash(ws, "approve", tid, "requirements")})
    assert la.ctx.ticket_option(tid, "notify") is True  # not offered, so not turned off


# --- human only --------------------------------------------------------------------------------------------------

def test_an_agent_cannot_set_an_option(live, ws, put, agent):
    from orch.addons import ticket_options
    from orch.errors import HumanOnlyError
    tid = put("backlog")
    with pytest.raises(HumanOnlyError):
        ticket_options.set_value(ws, tid, "hello-status", "notify", True, agent)
    assert not _opt_path(ws).exists()


def test_the_cli_set_refuses_an_agent_harness_and_list_is_open(live, ws, put, monkeypatch, capsys):
    import orch.actor
    from orch.cli import run
    tid = put("backlog")
    monkeypatch.setenv("CLAUDECODE", "1")
    assert run(["addon", "ticket-option", "list", tid, "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["options"][0] == {"option": "hello-status/notify", "label": OPTION["label"], "value": False}
    assert run(["addon", "ticket-option", "set", tid, "hello-status/notify", "on"]) != 0
    assert not _opt_path(ws).exists()


def test_the_guard_denies_an_agent_running_ticket_option_set():
    from orch.hooks import guard
    assert guard._ADDON_ADMIN.search("orch addon ticket-option set L-0001 orch-tix/notify on")
    assert not guard._ADDON_ADMIN.search("orch addon ticket-option list L-0001")


def test_addon_can_only_relay_its_own_declared_option(live, ws, put):
    tid = put("backlog")
    la = ws.addons.get("hello-status")
    ops = la.ctx.ops()
    assert ops.relay_ticket_option(tid, "notify", True) is True
    assert la.ctx.ticket_option(tid, "notify") is True
    from orch.errors import UsageError
    with pytest.raises(UsageError):
        ops.relay_ticket_option(tid, "other", True)
    from orch.core.events import read_events
    assert [e.via for e in read_events(ws, tid) if e.kind == "ticket.option"] == ["addon:hello-status"]


# --- CSRF --------------------------------------------------------------------------------------------------------

def test_cross_origin_option_post_is_refused(live, ws, put):
    tid = put("backlog")
    r = live.post(f"/t/{tid}/option", data={"option": "hello-status/notify", "value": "1"},
                  headers={"origin": "https://evil.example"}, follow_redirects=False)
    assert r.status_code == 403
    assert not _opt_path(ws).exists()


def test_option_post_needs_the_dashboard_token(ws, option_addon, put):
    from fastapi.testclient import TestClient
    from orch.dashboard.app import create_app
    _enable(ws, option_addon)
    tid = put("backlog")
    r = TestClient(create_app(ws, "tok")).post(f"/t/{tid}/option", data={"option": "hello-status/notify", "value": "1"})
    assert r.status_code in (401, 403) and not _opt_path(ws).exists()
