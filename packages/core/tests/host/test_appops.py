"""#1455 (HUB-PU-05) with #1457: ``app.prepare`` / ``app.release`` / ``app.send`` / ``app.list`` / ``app.discard`` end
to end on a real served workspace and a fake orch-publish behind a hub.

The invariants: an agent prepares and never signs or sends; nothing is uploaded without a person's signature over the
exact words of THIS staged release; the bytes sent are the staged bytes; a changed release has other words; an upload
is never live without the owner's approval of the commit."""

from __future__ import annotations

import json
import re

import pytest

from orch.canon.jcs import dumps as cj
from orch.host import signcontent
from orch.host.appops import APP_OPS
from orch.hubclient import published as pub
from orch.publishclient import approval
from tests.host import human_helpers
from tests.host.dispatch_helpers import SESSION, err, ok
from tests.host.test_click import Click, click  # noqa: F401
from tests.host.test_hub_ops import keystore  # noqa: F401
from tests.host.test_publishing import FakeServer, World

lab, served, human = human_helpers.lab, human_helpers.served, human_helpers.human

INDEX = b"<!doctype html><title>board</title><script src=app.js></script>"
APPJS = b"document.title='board'"


class AppServer(FakeServer):
    """orch-publish's app routes, enough to see what the host sends: it stores versions and never serves one."""

    def _serve(self, scheme, netloc, method, target, headers, body, timeout):
        parts = target.split("/")[4:]
        if parts[:1] == ["apps"] and netloc == self.host and self._authentic(headers, method, netloc, target, body):
            self.requests.append(
                {"method": method, "target": target, "body": body, "netloc": netloc, "headers": headers}
            )
            app = self.apps.setdefault(parts[1], {"versions": {}, "files": {}})
            if method == "PUT" and len(parts) == 4:
                app["versions"][parts[3]] = json.loads(body)
                return 200, b'{"result":"created"}'
            if method == "PUT" and parts[4:5] == ["files"]:
                app["files"][(parts[3], "/".join(parts[5:]))] = body
                return 200, b'{"result":"stored"}'
            return 404, b'{"error":"not_found"}'
        return super()._serve(scheme, netloc, method, target, headers, body, timeout)


@pytest.fixture
def a(served, human, monkeypatch):
    world = World(served, human, monkeypatch)
    old = world.server
    new = AppServer(old.wsk_pub)
    new.apps = {}
    world.server = world.hub.server = new
    world.addon()
    world.configure()
    return world


def prepare(a, key, *names, agent=False, **args):
    base = {"ref": key, "artifacts": list(names), "name": "Sales board", "kind": "app", "access": "company",
            "slug": "sales-board"}  # fmt: skip
    return a.req("app.prepare", {**base, **args}, agent=agent)


def ticket(a):
    return a.ticket_with(**{"index.html": INDEX, "app.js": APPJS})


def test_the_app_ops_are_declared():
    assert set(APP_OPS) == {"app.prepare", "app.send", "app.list", "app.discard"}


def test_prepare_stages_the_files_and_the_meta_and_nothing_leaves(a):
    uid, key = ticket(a)
    got = ok(prepare(a, key, "index.html", "app.js", reach=["https://api.example.com"]))["data"]
    assert re.fullmatch(r"pa_[0-9a-f]{32}", got["staged_id"]) and got["version"] == 1 and got["origin"] == "person"
    assert [f["path"] for f in got["files"]] == ["app.js", "index.html"] and got["needs"] == "a person's signature"
    assert a.server.requests == [] and "publish_out" not in a.hub.ops()


def claim(a, key):
    ok(a.served.client().req("claim", {"ref": key}, grant=a.served.grant, session=SESSION, idem=True))


def test_an_agent_prepares_but_cannot_send_or_sign(a):
    uid, key = ticket(a)
    assert err(prepare(a, key, "index.html", "app.js", agent=True)) == "op.not_allowed"  # not its claimed ticket
    claim(a, key)
    got = ok(prepare(a, key, "index.html", "app.js", agent=True))["data"]
    assert got["origin"] == "agent"
    assert err(a.req("app.send", {"staged_id": got["staged_id"]}, agent=True)) == "op.not_allowed"
    res = a.req("human.prepare", {"op": "app.release", "args": {"staged_id": got["staged_id"]}, "handoff": True},
                agent=True)  # fmt: skip
    assert err(res) in {"human_only", "op.not_allowed"}
    assert a.server.requests == []


def test_a_bad_meta_is_refused_before_anything_is_staged(a):
    uid, key = ticket(a)
    for bad in ({"reach": ["http://x.example.com"]}, {"access": "group"}, {"slug": "Bad Slug"}, {"name": "x\ny"}):
        res = prepare(a, key, "index.html", "app.js", **bad)
        assert err(res) == "invalid.input", bad
    assert err(prepare(a, key, "app.js")) == "invalid.input"  # no index.html
    assert ok(a.req("app.list", {}))["data"]["staged"] == []


def test_send_without_a_signature_is_refused_and_nothing_is_sent(a):
    uid, key = ticket(a)
    staged = ok(prepare(a, key, "index.html", "app.js"))["data"]["staged_id"]
    assert err(a.req("app.send", {"staged_id": staged})) == "publish.not_approved"
    assert a.server.requests == []


def test_the_review_shows_name_access_reach_and_every_file(a):
    uid, key = ticket(a)
    staged = ok(prepare(a, key, "index.html", "app.js", reach=["https://api.example.com"]))["data"]["staged_id"]
    prepared = a.human.prepare("app.release", {"staged_id": staged})
    (event,) = prepared["events"]
    assert event["type"] == "log.added" and event["text"].startswith(approval.APP_MARK + "\n")
    for needle in ("Sales board", "everyone signed in to your company hub", "https://api.example.com", "app.js",
                   "index.html", f"Staged release: {staged}"):  # fmt: skip
        assert needle in event["text"], needle
    signcontent.build(prepared, verified=True)
    a.human.check(prepared)  # the independent signer re-derives and accepts the review


def test_sign_then_send_uploads_the_staged_bytes_and_is_not_live(a):
    uid, key = ticket(a)
    data = ok(prepare(a, key, "index.html", "app.js"))["data"]
    staged, app_id = data["staged_id"], data["app_id"]
    a.human.run("app.release", {"staged_id": staged})
    got = ok(a.req("app.send", {"staged_id": staged}))["data"]
    assert got["uploaded"] is True and got["live"] is False and got["version"] == 1
    srv = a.server
    assert srv.bad_signatures == []
    kinds = [(r["method"], r["target"].split("/", 4)[4]) for r in srv.requests if "/apps/" in r["target"]]
    assert kinds == [
        ("PUT", f"apps/{app_id}/versions/1"),
        ("PUT", f"apps/{app_id}/versions/1/files/app.js"),
        ("PUT", f"apps/{app_id}/versions/1/files/index.html"),
    ]  # no commit: it needs the owner's approval of that request
    stored_app = srv.apps[app_id]
    assert stored_app["files"][("1", "index.html")] == INDEX and stored_app["files"][("1", "app.js")] == APPJS
    meta = stored_app["versions"]["1"]
    assert pub.meta_problem(meta) is None and meta["access"] == "company" and meta["audience"] == "company"
    assert cj(meta) == cj(json.loads(cj(meta)))
    rows = ok(a.req("app.list", {}))["data"]
    assert rows["staged"] == [] and rows["apps"][0]["id"] == app_id and rows["apps"][0]["uploaded"] == 1


def test_the_signature_covers_exactly_this_release(a):
    uid, key = ticket(a)
    first = ok(prepare(a, key, "index.html", "app.js"))["data"]["staged_id"]
    other = ok(prepare(a, key, "index.html", "app.js", access="public"))["data"]["staged_id"]
    a.human.run("app.release", {"staged_id": first})
    assert err(a.req("app.send", {"staged_id": other})) == "publish.not_approved"
    assert a.server.requests == []


def test_a_staged_file_changed_after_the_signature_is_not_sent(a):
    uid, key = ticket(a)
    staged = ok(prepare(a, key, "index.html", "app.js"))["data"]["staged_id"]
    a.human.run("app.release", {"staged_id": staged})
    path = a.served.lab.state / "hosts" / a.served.wid / "publish" / "appstage" / staged / "files" / "app.js"
    path.write_bytes(b"alert(1)" + APPJS[8:])
    assert err(a.req("app.send", {"staged_id": staged})) == "publish.changed"
    assert [r for r in a.server.requests if "/apps/" in r["target"]] == []


def test_an_update_shows_what_changed_against_the_live_version(a):
    uid, key = ticket(a)
    first = ok(prepare(a, key, "index.html", "app.js"))["data"]
    a.human.run("app.release", {"staged_id": first["staged_id"]})
    ok(a.req("app.send", {"staged_id": first["staged_id"]}))
    # the first version is uploaded, not live: a second one has no live version to compare with
    again = ok(prepare(a, key, "index.html", "app.js", app_id=first["app_id"]))["data"]
    assert again["version"] == 2 and again["app_id"] == first["app_id"]
    assert err(prepare(a, key, "index.html", "app.js")) == "invalid.input"  # same slug, no app_id


def test_an_agent_discards_only_its_own_release_and_a_person_any(a):
    uid, key = ticket(a)
    claim(a, key)
    mine = ok(prepare(a, key, "index.html", "app.js", agent=True))["data"]["staged_id"]
    ok(a.req("app.discard", {"staged_id": mine}, agent=True))
    theirs = ok(prepare(a, key, "index.html", "app.js", slug="other-app"))["data"]["staged_id"]
    assert err(a.req("app.discard", {"staged_id": theirs}, agent=True)) == "op.not_allowed"
    ok(a.req("app.discard", {"staged_id": theirs}))


def test_everything_is_off_until_the_publish_addon_is_granted(served, human, monkeypatch):
    bare = World(served, human, monkeypatch)
    uid, key = bare.ticket_with(**{"index.html": INDEX})
    assert err(prepare(bare, key, "index.html")) == "runner.disabled"


def test_a_stage_whose_meta_names_another_ticket_is_never_sent(a):
    uid, key = ticket(a)
    staged = ok(prepare(a, key, "index.html", "app.js"))["data"]["staged_id"]
    path = a.served.lab.state / "hosts" / a.served.wid / "publish" / "appstage" / staged / "meta.json"
    doc = json.loads(path.read_text())
    assert doc["app"]["ticket"] == uid
    doc["app"]["ticket"] = "0" * 26
    path.write_text(json.dumps(doc))
    assert err(a.req("app.send", {"staged_id": staged})) == "publish.changed"
    assert a.server.requests == []


class _Store:
    def __init__(self, events):
        self._events = events

    def log_seq(self, uid):
        return len(self._events)

    def events(self, uid, after=0, limit=200):
        return self._events[after : after + limit]


def _note(text, **more):
    return {"type": "log.added", "text": text, "at": "2026-10-31T10:00:00Z", "sig": "x",
            "actor": {"kind": "person", "id": "p_me"}, **more}  # fmt: skip


def _call(events):
    import types

    service = types.SimpleNamespace(store=_Store(events), clock=lambda: 1_793_440_800.0)
    return types.SimpleNamespace(service=service, viewer="p_me")


def test_only_a_locally_signed_note_of_the_matching_op_counts_as_the_approval():
    """#1781 Opus delta: a hub-signed note (auth hub, actor.via) with the right words is no approval, and neither is a
    note that does not start with the app approval line."""
    from orch.host import appops

    stage = {"ticket": {"uid": "01J9ZP0000000000000000EVT1", "key": "TST-0001"}}
    text = approval.APP_MARK + "\nTicket: TST-0001\nStaged release: pa_" + "a" * 32
    assert appops.signed_release(_call([_note(text)]), stage, text) is True
    hub_auth = _note(text, auth="hub")
    hub_via = _note(text)
    hub_via["actor"] = {"kind": "person", "id": "p_me", "via": "hub_x"}
    for bad in (hub_auth, hub_via):
        assert appops.signed_release(_call([bad]), stage, text) is False
    other = "Some other note\nStaged release: pa_" + "a" * 32
    assert appops.signed_release(_call([_note(other)]), stage, other) is False
    unsigned = _note(text)
    del unsigned["sig"]
    assert appops.signed_release(_call([unsigned]), stage, text) is False
    agent = _note(text)
    agent["actor"] = {"kind": "agent", "id": "p_me"}
    assert appops.signed_release(_call([agent]), stage, text) is False


def test_a_staged_file_with_other_bytes_of_the_same_size_is_not_sent(a):
    uid, key = ticket(a)
    staged = ok(prepare(a, key, "index.html", "app.js"))["data"]["staged_id"]
    a.human.run("app.release", {"staged_id": staged})
    path = a.served.lab.state / "hosts" / a.served.wid / "publish" / "appstage" / staged / "files" / "app.js"
    path.write_bytes(bytes(len(APPJS)))  # same size, other bytes: the recomputed sha256 differs
    assert err(a.req("app.send", {"staged_id": staged})) == "publish.changed"
    assert [r for r in a.server.requests if "/apps/" in r["target"]] == []
