"""The Remote tab (Workspace & addons): every route and state, human-only, the Reject default, the one-time link,
scope lowering, revoke (linked pair included), the kill switch, a damaged registry, hostile text, never-remote."""
import os
import re
from pathlib import Path
from urllib.parse import unquote_plus

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("cryptography")

from fastapi.testclient import TestClient  # noqa: E402

from orch import actor  # noqa: E402
from orch.dashboard import remote_gate, views  # noqa: E402
from orch.dashboard.app import create_app  # noqa: E402
from orch.remote import store as phone_store  # noqa: E402
from orch.remote.bridge_host import files, keys, signatures  # noqa: E402
from orch.remote.bridge_host.host_check import Host, load_host_key  # noqa: E402
from orch.remote.bridge_host.pairing import Pending  # noqa: E402
from orch.remote.bridge_host.registry import Device, Registry  # noqa: E402
from orch.remote.bridge_link import NullLink  # noqa: E402
from test_remote_scopes import call, origin  # noqa: E402
from orch.dashboard.reach import Scope  # noqa: E402

ORIGIN = {"origin": "http://testserver"}
WS = bytes(range(16))
NOW = 1_790_000_000_000


class FakeLink:
    def __init__(self, state="online", error=None):
        self.st, self.err, self.cut = state, error, 0

    def status(self):
        return {"state": self.st, "since": 1.0, "last_error": self.err, "host_online": self.st == "online"}

    def disconnect(self):
        self.cut += 1


def new_pub():
    return signatures.public_bytes(signatures.generate())


def device(pub, scope="look", label="Laptop", phone_link=None):
    return Device(keys.device_id(WS, pub).hex(), pub, scope, label, NOW, phone_link=phone_link)


@pytest.fixture
def cfg(tmp_path):
    return tmp_path / "cfg"


@pytest.fixture
def host(cfg):
    root = files.bridge_dir(cfg, WS.hex())
    return Host(workspace=WS, k_ws=bytes(32), host_key=load_host_key(root), root=root, clock=lambda: NOW,
                route=lambda m, d: None, phone_key=lambda p: None, rp_id="tix.example", origin="https://tix.example")


@pytest.fixture
def link():
    return FakeLink()


@pytest.fixture
def app(ws, host, link, monkeypatch):
    monkeypatch.setattr(views, "_setup_count", lambda ws, checks=None: 0)
    a = create_app(ws, "tok")
    a.state.bridge_host, a.state.bridge_link = host, link
    return a


@pytest.fixture
def client(app):
    c = TestClient(app)
    c.get("/?token=tok")
    return c


def tab(client):
    return client.get("/workspace?tab=remote").text


def post(client, path, **data):
    r = client.post(path, data=data, headers=ORIGIN, follow_redirects=False)
    if "location" in r.headers:
        r.headers["location"] = unquote_plus(r.headers["location"])  # read the message, not its encoding
    return r


def pend(host, label="Pixel 8", scope="type"):
    pub = new_pub()
    did = keys.device_id(WS, pub).hex()
    host.pairing.pending[did] = Pending(pub, "ab" * 16, scope, None, label)
    return did, keys.device_fingerprint(pub)


# -- states --------------------------------------------------------------------------------------------------------

def test_the_tab_sits_next_to_phones(client):
    html = client.get("/workspace").text
    assert re.findall(r'data-tab="(\w+)"', html[html.index('<nav class="tabs ws-tabs"'):])[3:5] == ["phones", "remote"]
    assert 'id="tab-remote" data-tab-panel="remote" hidden' in html


def test_not_running_says_so_and_is_read_only(ws, monkeypatch, cfg):
    monkeypatch.setattr(views, "_setup_count", lambda ws, checks=None: 0)
    app = create_app(ws, "tok")
    reg = Registry(files.bridge_dir(cfg, WS.hex()), WS)
    reg.add(device(new_pub(), "decide", "Old laptop"), NOW)
    app.state.bridge_registry = reg
    c = TestClient(app)
    c.get("/?token=tok")
    html = tab(c)
    assert "Remote is not running" in html and "--remote" in html
    assert "Old laptop" in html and "Add a device" not in html and "/revoke" not in html
    assert "Not connected" in html
    r = post(c, "/workspace/remote/offer", scope="look")
    assert "Remote is not running" in r.headers["location"]
    assert len(reg.devices()) == 1


def test_not_running_without_any_registry(ws, monkeypatch):
    monkeypatch.setattr(views, "_setup_count", lambda ws, checks=None: 0)
    c = TestClient(create_app(ws, "tok"))
    c.get("/?token=tok")
    assert "Remote is not running" in tab(c) and "No remote device is paired yet" in tab(c)


@pytest.mark.parametrize("state,text", [("online", "Online"), ("connecting", "Connecting"),
                                        ("reconnecting", "Reconnecting"), ("stopped", "Disconnected by you")])
def test_link_states(client, link, state, text):
    link.st = state
    assert text in tab(client)


def test_error_code_becomes_a_sentence_and_the_fix(client, link):
    link.st, link.err = "error", "host_taken"
    html = tab(client)
    assert "already holds this workspace" in html and "host_taken" not in html
    link.err = "<b>weird</b>"
    assert "&lt;b&gt;" not in html and "<b>weird</b>" not in tab(client)
    link.err = "new_code"
    assert "code: new_code" in tab(client)


def test_a_damaged_registry_is_shown_prominently(client, host):
    host.registry.path.write_bytes(b"{not json")
    html = tab(client)
    assert "callout-err" in html and "cannot be read" in html and "Add a device" not in html


def test_damaged_request_records_are_shown(client, host):
    host.store.damaged.add("ff" * 16)
    assert "1 stored request record(s) are damaged" in tab(client)


def test_devices_list_shows_fields(client, host):
    pub = new_pub()
    host.registry.add(device(pub, "operate", "Pixel 8", phone_link="ph_1"), NOW)
    host.store.save_seq(keys.device_id(WS, pub).hex(), 3, 1)
    html = tab(client)
    assert "Pixel 8" in html and keys.device_fingerprint(pub) in html
    assert "linked to a phone pairing" in html and "Editing tickets can steer running agents; agents read ticket text." in html
    assert "Type switch" in html and "UTC" in html


def test_never_seen_device(client, host):
    host.registry.add(device(new_pub()), NOW)
    assert "never" in tab(client)


# -- pairing -------------------------------------------------------------------------------------------------------

def test_offer_shows_the_link_once(client, host):
    r = post(client, "/workspace/remote/offer", scope="decide")
    loc = r.headers["location"]
    assert "#" in loc and "v1." not in loc
    first = client.get(loc.split("#")[0]).text
    assert "https://tix.example/remote/pair#v1." in first and "<svg" in first and "10 minutes" in first
    assert "Copy pairing link" in first
    second = client.get(loc.split("#")[0]).text
    assert "v1." not in second and "Copy pairing link" not in second
    assert "v1." not in tab(client)
    assert len(host.pairing.offers) == 1 and next(iter(host.pairing.offers.values())).scope == "decide"


def test_offer_response_is_not_cached(client):
    loc = post(client, "/workspace/remote/offer", scope="look").headers["location"].split("#")[0]
    r = client.get(loc)
    assert r.headers["cache-control"] == "no-store" and "etag" not in r.headers


def test_a_foreign_token_is_not_burned_by_offer(client):
    reveals = client.app.state.reveals
    token = reveals.put({"kind": "reveal", "label": "x", "text": "shh"})
    assert "shh" not in client.get(f"/workspace?tab=remote&offer={token}").text
    assert reveals.peek(token) is not None


def test_type_offer_needs_its_switch(client, host):
    assert "Type also needs" in post(client, "/workspace/remote/offer", scope="type").headers["location"]
    assert not host.pairing.offers
    assert "offer=" in post(client, "/workspace/remote/offer", scope="type", allow_type="1").headers["location"]
    assert "choose a scope" in post(client, "/workspace/remote/offer", scope="root").headers["location"]


def test_pending_shows_fingerprint_and_reject_comes_first(client, host):
    did, fp = pend(host, "Pixel 8", "operate")
    html = tab(client)
    assert fp in html and "Pixel 8" in html and "used by someone else" in html
    assert html.index("/reject") < html.index("/approve")
    assert re.search(r'class="btn btn-primary">Reject', html)
    assert "Not linked to an existing phone pairing" in html
    assert did not in {d for d in host.registry.devices()}  # showing it adds nothing


def test_approve_needs_the_last_group(client, host):
    did, fp = pend(host, "Pixel 8", "operate")
    r = post(client, f"/workspace/remote/pending/{did}/approve", scope="operate", last_group="ZZZZ")
    assert "not the last group" in r.headers["location"] and not host.registry.devices()
    r = post(client, f"/workspace/remote/pending/{did}/approve", scope="operate", last_group="")
    assert not host.registry.devices()
    r = post(client, f"/workspace/remote/pending/{did}/approve", scope="operate", last_group=fp[-4:].lower())
    assert "added" in r.headers["location"]
    dev = host.registry.get(did)
    assert dev.scope == "operate" and dev.label == "Pixel 8"
    assert [e["event"] for e in host.registry.audit_entries()][0] == "added"


def test_approval_only_lowers_the_scope(client, host):
    did, fp = pend(host, "Pixel 8", "decide")
    r = post(client, f"/workspace/remote/pending/{did}/approve", scope="operate", last_group=fp[-4:])
    assert "not approved" in r.headers["location"] and not host.registry.devices()
    post(client, f"/workspace/remote/pending/{did}/approve", scope="look", last_group=fp[-4:])
    assert host.registry.get(did).scope == "look"


def test_approving_type_needs_its_switch(client, host):
    did, fp = pend(host, "Pixel 8", "type")
    r = post(client, f"/workspace/remote/pending/{did}/approve", scope="type", last_group=fp[-4:])
    assert "Type also needs" in r.headers["location"] and host.registry.get(did) is None  # never implied by the offer
    assert host.pairing.pending[did].state == "pending"
    post(client, f"/workspace/remote/pending/{did}/approve", scope="type", last_group=fp[-4:], allow_type="1")
    assert host.registry.get(did).scope == "type"
    did2, fp2 = pend(host, "Pixel 9", "decide")
    r = post(client, f"/workspace/remote/pending/{did2}/approve", scope="type", last_group=fp2[-4:])
    assert host.registry.get(did2) is None


def test_reject_is_audited_and_nothing_is_added(client, host):
    did, _ = pend(host)
    r = post(client, f"/workspace/remote/pending/{did}/reject")
    assert "rejected" in r.headers["location"]
    assert not host.registry.devices() and host.pairing.pending[did].state == "rejected"
    assert "Pairing rejected" in tab(client)
    assert "no pending" in post(client, f"/workspace/remote/pending/{did}/reject").headers["location"]
    assert "no pending" in post(client, f"/workspace/remote/pending/{did}/approve", last_group="AAAA").headers["location"]


# -- scope, revoke, kill switch ------------------------------------------------------------------------------------

def test_scope_change_is_audited(client, host):
    d = host.registry.add(device(new_pub(), "look"), NOW)
    post(client, f"/workspace/remote/devices/{d.id}/scope", scope="operate")
    assert host.registry.get(d.id).scope == "operate"
    assert host.registry.audit_entries()[0]["event"] == "scope_changed"
    assert "Scope changed" in tab(client)


def test_type_scope_needs_the_switch(client, host):
    d = host.registry.add(device(new_pub(), "look"), NOW)
    r = post(client, f"/workspace/remote/devices/{d.id}/scope", scope="type")
    assert "Type also needs" in r.headers["location"] and host.registry.get(d.id).scope == "look"
    post(client, f"/workspace/remote/devices/{d.id}/scope", scope="type", allow_type="1")
    assert host.registry.get(d.id).scope == "type"
    post(client, f"/workspace/remote/devices/{d.id}/scope", scope="decide")  # lowering never needs it
    assert host.registry.get(d.id).scope == "decide"


def test_scope_change_refuses_unknown_and_revoked(client, host):
    assert "no such" in post(client, "/workspace/remote/devices/" + "0" * 32 + "/scope", scope="look").headers["location"]
    d = host.registry.add(device(new_pub()), NOW)
    host.revoke(d.id)
    assert "no such" in post(client, f"/workspace/remote/devices/{d.id}/scope", scope="decide").headers["location"]


def test_revoke_asks_first_then_revokes_everywhere(client, host, cfg):
    pub = new_pub()
    d = host.registry.add(device(pub, "decide", "Pixel 8"), NOW)
    other = bytes(range(16, 32))
    other_reg = Registry(files.bridge_dir(cfg, other.hex()), other)
    od = other_reg.add(Device(keys.device_id(other, pub).hex(), pub, "look", "Pixel 8", NOW), NOW)
    page = post(client, f"/workspace/remote/devices/{d.id}/revoke", ask="1")
    assert page.status_code == 200 and "Revoke device" in page.text and "Revoke Pixel 8?" in page.text
    assert not host.registry.get(d.id).revoked
    r = post(client, f"/workspace/remote/devices/{d.id}/revoke")
    assert "revoked" in r.headers["location"].lower()
    assert host.registry.get(d.id).revoked and other_reg.get(od.id).revoked
    again = post(client, f"/workspace/remote/devices/{d.id}/revoke")  # idempotent: it only re-runs the other steps
    assert "Device revoked" in again.headers["location"] and "err=" not in again.headers["location"]
    assert "no such device" in post(client, "/workspace/remote/devices/" + "0" * 32 + "/revoke").headers["location"]
    assert "Revoked: Pixel 8" in tab(client) and "Finish revoking" in tab(client)


def test_revoking_a_device_revokes_its_linked_phone(client, host, ws):
    phone, _ = phone_store.pair(ws.root, label="iPhone", addon="x")
    d = host.registry.add(device(new_pub(), "decide", "iPhone", phone_link=phone.id), NOW)
    assert "linked phone pairing" in post(client, f"/workspace/remote/devices/{d.id}/revoke", ask="1").text
    post(client, f"/workspace/remote/devices/{d.id}/revoke")
    assert phone_store.find(ws.root, phone.id).revoked_at is not None


def test_revoking_the_phone_revokes_its_linked_device(client, host, ws):
    phone, _ = phone_store.pair(ws.root, label="iPhone", addon="x")
    d = host.registry.add(device(new_pub(), "decide", "iPhone", phone_link=phone.id), NOW)
    keep = host.registry.add(device(new_pub(), "look", "Other"), NOW)
    post(client, f"/workspace/phones/{phone.id}/revoke")
    assert host.registry.get(d.id).revoked and not host.registry.get(keep.id).revoked


def test_kill_switch_asks_then_disconnects(client, host, link):
    assert "Disconnect remote now" in tab(client)
    page = post(client, "/workspace/remote/disconnect", ask="1")
    assert "Disconnect remote" in page.text and link.cut == 0 and not host.stopped
    post(client, "/workspace/remote/disconnect")
    assert link.cut == 1 and host.stopped


def test_kill_switch_works_with_a_link_and_no_host(ws, monkeypatch):
    monkeypatch.setattr(views, "_setup_count", lambda ws, checks=None: 0)
    app = create_app(ws, "tok")
    app.state.bridge_link = FakeLink()
    c = TestClient(app)
    c.get("/?token=tok")
    post(c, "/workspace/remote/disconnect")
    assert app.state.bridge_link.cut == 1


# -- human-only, never remote --------------------------------------------------------------------------------------

POSTS = ["/workspace/remote/offer", "/workspace/remote/disconnect", "/workspace/remote/pending/" + "a" * 32 + "/approve",
         "/workspace/remote/pending/" + "a" * 32 + "/reject", "/workspace/remote/devices/" + "a" * 32 + "/scope",
         "/workspace/remote/devices/" + "a" * 32 + "/revoke"]


@pytest.mark.parametrize("path", POSTS)
def test_posts_need_the_same_origin(client, host, link, path):
    assert client.post(path, data={"scope": "look"}, follow_redirects=False).status_code == 403
    assert link.cut == 0 and not host.pairing.offers


@pytest.mark.parametrize("path", POSTS)
def test_an_agent_harness_is_refused(client, host, link, monkeypatch, path):
    host.registry.add(device(new_pub()), NOW)
    monkeypatch.setattr(actor, "process_chain", lambda: [(100, "bash"), (101, "/opt/homebrew/bin/claude --x")])
    r = post(client, path, scope="look", ask="")
    assert r.status_code == 403 and "human-only" in r.text
    assert link.cut == 0 and not host.pairing.offers and not host.stopped
    assert len(host.registry.devices()) == 1 and not any(d.revoked for d in host.registry.devices().values())


@pytest.mark.parametrize("path", POSTS)
def test_a_remote_request_gets_the_uniform_refusal(app, host, link, path):
    for scope in (Scope.LOOK, Scope.TYPE):
        status, _, body = call(app, "POST", path, remote=origin(scope, fresh=True),
                               body=b"scope=look&allow_type=1", headers=(("content-type", "application/x-www-form-urlencoded"),))
        assert status == 403 and remote_gate.NO_WAY.encode() in body
    assert link.cut == 0 and not host.pairing.offers and not host.stopped


def test_the_tab_itself_is_never_remote(app):
    status, _, body = call(app, "GET", "/workspace", remote=origin(Scope.TYPE, fresh=True), query=b"tab=remote")
    assert status == 403 and b"Disconnect" not in body


def test_every_new_route_is_tagged_never_remote():
    for p in POSTS:
        key = ("POST", re.sub(r"/a{32}/", "/{did}/", p))
        assert remote_gate.TAGS[key].scope is remote_gate.NEVER, key


def test_devices_are_added_only_from_the_remote_tab():
    """The registry's write API is called from nowhere else: not another route, not the CLI."""
    src = Path(__file__).resolve().parent.parent / "src" / "orch"
    pattern = re.compile(r"\b(?:host|pairing|registry)\.(?:approve|add)\(")
    users = {p.relative_to(src).as_posix() for p in src.rglob("*.py") if pattern.search(p.read_text(encoding="utf-8"))}
    assert users <= {"dashboard/routes_remote.py", "remote/bridge_host/host_check.py", "remote/bridge_host/pairing.py"}, users
    assert "dashboard/routes_remote.py" in users


def test_no_window_dialogs_in_templates():
    tpl = Path(__file__).resolve().parent.parent / "src" / "orch" / "dashboard" / "templates"
    text = (tpl / "_remote.html").read_text(encoding="utf-8")
    assert not re.search(r"\b(?:confirm|alert|prompt)\s*\(|window\.open", text)


# -- hostile text --------------------------------------------------------------------------------------------------

def test_hostile_labels_and_subjects_are_text_only(client, host):
    did, _ = pend(host, '<script>alert(1)</script>"><img src=x onerror=1>' + "x" * 100)
    html = tab(client)
    assert "<script>alert" not in html and "<img src=x" not in html and 'onerror=1>' not in html
    pub = new_pub()
    d = host.registry.add(device(pub, "look", '<b onmouseover=1>evil</b>'), NOW)
    host.registry.audit(NOW, "assertion", device=d.id, ok=True, why=None, rid="a" * 32, purpose="fresh", scope="type",
                        subject={"kind": "charter", "shown": "Start <img src=x onerror=2> epic", "digest": ""})
    host.registry.audit(NOW, "<i>strange</i>", device=d.id)
    html = tab(client)
    assert "<img src=x onerror=2>" not in html and "&lt;img src=x onerror=2&gt;" in html
    assert "<b onmouseover" not in html and "<i>strange" not in html
    assert "Confirmed on" in html and "charter" in html


def test_labels_are_limited_to_the_safe_set_and_40_characters():
    from orch.dashboard.routes_remote import safe_label
    assert safe_label("a<b>‮ c.d_e-f") == "ab c.d_e-f"
    assert len(safe_label("x" * 100)) == 40


def test_activity_lists_added_and_assertions(client, host):
    pub = new_pub()
    d = host.registry.add(device(pub, "type", "Mac"), NOW)
    host.registry.audit(NOW, "assertion", device=d.id, ok=False, why="bad_signature", rid="a" * 32)
    html = tab(client)
    assert "Device added: Mac" in html and "Confirmation refused on Mac" in html and "bad_signature" in html


def _fail_once(monkeypatch, module, name):
    real, calls = getattr(module, name), []

    def flaky(*a, **k):
        calls.append(1)
        if len(calls) == 1:
            raise OSError("disk trouble")
        return real(*a, **k)
    monkeypatch.setattr(module, name, flaky)


def test_a_half_finished_revoke_is_completed_by_a_retry(client, host, ws, cfg, monkeypatch):
    from orch.dashboard import routes_remote
    from orch.remote.bridge_host import registry as registry_mod
    pub = new_pub()
    phone, _ = phone_store.pair(ws.root, label="iPhone", addon="x")
    d = host.registry.add(device(pub, "decide", "iPhone", phone_link=phone.id), NOW)
    other = bytes(range(16, 32))
    other_reg = Registry(files.bridge_dir(cfg, other.hex()), other)
    od = other_reg.add(Device(keys.device_id(other, pub).hex(), pub, "look", "iPhone", NOW), NOW)
    _fail_once(monkeypatch, registry_mod, "revoke_everywhere")  # step 2 fails the first time
    r = post(client, f"/workspace/remote/devices/{d.id}/revoke")
    loc = r.headers["location"]
    assert "not fully revoked" in loc and "other workspaces" in loc and "Finish revoking" in loc
    assert "revoked here" in loc  # what did work is reported too
    assert host.registry.get(d.id).revoked and not other_reg.get(od.id).revoked
    assert phone_store.find(ws.root, phone.id).revoked_at is not None  # step 3 still ran
    assert "Finish revoking" in tab(client)
    again = post(client, f"/workspace/remote/devices/{d.id}/revoke")
    assert "err=" not in again.headers["location"] and other_reg.get(od.id).revoked
    assert "Finish revoking" in post(client, f"/workspace/remote/devices/{d.id}/revoke", ask="1").text


def test_a_failed_phone_step_is_shown_and_retried(client, host, ws, monkeypatch):
    phone, _ = phone_store.pair(ws.root, label="iPhone", addon="x")
    d = host.registry.add(device(new_pub(), "decide", "iPhone", phone_link=phone.id), NOW)
    _fail_once(monkeypatch, phone_store, "revoke")
    assert "linked phone pairing" in post(client, f"/workspace/remote/devices/{d.id}/revoke").headers["location"]
    assert phone_store.find(ws.root, phone.id).revoked_at is None
    post(client, f"/workspace/remote/devices/{d.id}/revoke")
    assert phone_store.find(ws.root, phone.id).revoked_at is not None


def test_damaged_other_workspaces_are_shown(client, host, cfg):
    d = host.registry.add(device(new_pub(), "decide", "Pixel 8"), NOW)
    bad = files.bridge_dir(cfg, bytes(range(32, 48)).hex())
    files.ensure_dir(bad)
    (bad / "registry.json").write_bytes(b"{broken")
    loc = post(client, f"/workspace/remote/devices/{d.id}/revoke").headers["location"]
    assert "cannot be read" in loc and "may still be live" in loc and host.registry.get(d.id).revoked


def test_reverse_path_shows_a_partial_failure(client, host, ws, monkeypatch):
    phone, _ = phone_store.pair(ws.root, label="iPhone", addon="x")
    a = host.registry.add(device(new_pub(), "decide", "First", phone_link=phone.id), NOW)
    b = host.registry.add(device(new_pub(), "decide", "Second", phone_link=phone.id), NOW)
    real = host.revoke

    def flaky(did):
        if did == a.id:
            raise OSError("disk trouble")
        return real(did)
    monkeypatch.setattr(host, "revoke", flaky)
    loc = post(client, f"/workspace/phones/{phone.id}/revoke").headers["location"]
    assert "Phone revoked" in loc and "First: could not revoke it here" in loc and "1 linked remote device" in loc
    assert not host.registry.get(a.id).revoked and host.registry.get(b.id).revoked


def test_reverse_path_without_a_host_uses_the_registry_file(ws, monkeypatch, cfg):
    monkeypatch.setattr(views, "_setup_count", lambda ws, checks=None: 0)
    app = create_app(ws, "tok")
    reg = Registry(files.bridge_dir(cfg, WS.hex()), WS)
    phone, _ = phone_store.pair(ws.root, label="iPhone", addon="x")
    d = reg.add(device(new_pub(), "decide", "iPhone", phone_link=phone.id), NOW)
    app.state.bridge_registry = reg
    c = TestClient(app)
    c.get("/?token=tok")
    loc = post(c, f"/workspace/phones/{phone.id}/revoke").headers["location"]
    assert "1 linked remote device" in loc and reg.get(d.id).revoked


def test_reverse_path_with_no_registry_says_it_could_not_look(client, ws, monkeypatch):
    monkeypatch.setattr(views, "_setup_count", lambda ws, checks=None: 0)
    app = create_app(ws, "tok")
    c = TestClient(app)
    c.get("/?token=tok")
    phone, _ = phone_store.pair(ws.root, label="iPhone", addon="x")
    loc = post(c, f"/workspace/phones/{phone.id}/revoke").headers["location"]
    assert "Phone revoked" in loc and "not checked" in loc


def test_the_gate_itself_refuses_a_remote_marker(app):
    """Directly, not through the remote gate's own refusal: the POST helper must refuse on its own."""
    from types import SimpleNamespace
    from orch.dashboard import reach, routes_remote
    req = SimpleNamespace(headers={"origin": "http://h", "host": "h"}, scope={reach.SCOPE_KEY: origin(Scope.TYPE)})
    assert routes_remote._gate(req).status_code == 403
    req.scope = {}
    assert routes_remote._gate(req) is None


def test_a_damaged_registry_shows_no_device_controls(client, host):
    d = host.registry.add(device(new_pub(), "type", "Pixel 8"), NOW)
    assert f"/devices/{d.id}/scope" in tab(client)
    host.registry.path.write_bytes(b"{not json")
    html = tab(client)
    assert "Pixel 8" not in html and "/devices/" not in html and "Save scope" not in html and "Revoke" not in html.replace(
        "Revoke device", "")
    assert "/workspace/remote/offer" not in html


def test_revoking_goes_through_the_host(client, host):
    """Host.revoke ends the device's lease and waiting work; the registry write alone would not."""
    d = host.registry.add(device(new_pub(), "type", "Pixel 8"), NOW)
    host.leases[d.id] = NOW + 60_000
    post(client, f"/workspace/remote/devices/{d.id}/revoke")
    assert d.id not in host.leases and host.registry.get(d.id).revoked


def test_the_default_link_is_off_without_the_loop(ws, monkeypatch):
    assert NullLink().status()["state"] == "off"
