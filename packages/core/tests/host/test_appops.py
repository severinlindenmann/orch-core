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
from orch.host import hubops, signcontent
from orch.host.appops import APP_OPS
from orch.hubclient import published as pub
from orch.publishclient import approval
from tests.host import human_helpers
from tests.host.dispatch_helpers import SESSION, err, ok
from tests.host.test_click import Click, click  # noqa: F401
from tests.host.test_hub_ops import (
    HUB_ID,
    keystore,  # noqa: F401
)
from tests.host.test_publishing import FakeServer, PublishHub, World

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
    assert set(APP_OPS) == {
        "app.prepare", "app.send", "app.list", "app.discard", "app.commit", "app.commit_request",
    }  # fmt: skip


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


# -- Opus review of #1784 ---------------------------------------------------------------------------------------------


def test_two_stages_of_one_app_the_second_send_is_refused(a):
    """F1: both stages were made against the same record; once the first is sent the second is stale, at the signature
    and at the send, and nothing of it is uploaded."""
    uid, key = ticket(a)
    first = ok(prepare(a, key, "index.html", "app.js"))["data"]
    a.human.run("app.release", {"staged_id": first["staged_id"]})
    ok(a.req("app.send", {"staged_id": first["staged_id"]}))
    one = ok(prepare(a, key, "index.html", "app.js", app_id=first["app_id"], name="Sales board 2"))["data"]
    two = ok(prepare(a, key, "index.html", "app.js", app_id=first["app_id"], name="Sales board 3"))["data"]
    assert one["version"] == 2 and two["version"] == 2
    a.human.run("app.release", {"staged_id": one["staged_id"]})
    a.human.run("app.release", {"staged_id": two["staged_id"]})  # both signed while the record was unchanged
    ok(a.req("app.send", {"staged_id": one["staged_id"]}))
    before = len(a.server.requests)
    assert err(a.req("app.send", {"staged_id": two["staged_id"]})) == "publish.changed"
    assert len(a.server.requests) == before
    assert err(a.human.prepare_res("app.release", {"staged_id": two["staged_id"]})) == "publish.changed"


def test_the_reach_may_not_name_the_publish_server_the_share_origin_or_the_hub(a):
    """F2: refused at prepare, before anybody signs."""
    uid, key = ticket(a)
    for origin in ("https://publish.example.org", "https://apps.example.org"):
        a.configure(server=origin, share=origin)
        res = prepare(a, key, "index.html", "app.js", reach=[origin])
        assert err(res) == "invalid.input", origin


def test_a_stale_staged_release_is_swept_at_the_next_prepare(a):
    """F3: a release no one finished with goes after a day, like a share."""
    import os

    from orch.host import publishing

    uid, key = ticket(a)
    old = ok(prepare(a, key, "index.html", "app.js"))["data"]["staged_id"]
    d = a.served.lab.state / "hosts" / a.served.wid / "publish" / "appstage" / old
    gone = a.served.service.clock() - publishing.STAGE_KEEP - 60
    os.utime(d, (gone, gone))
    ok(prepare(a, key, "index.html", "app.js", slug="other-app"))
    assert not d.exists()


def test_discard_is_for_the_person_who_prepared_it_or_one_who_may_release_it(a):
    """F4: seeing the ticket is not enough (``_may_share``)."""
    from orch.host import appops, publishing

    uid, key = ticket(a)
    staged = ok(prepare(a, key, "index.html", "app.js"))["data"]["staged_id"]
    stage = appops._load_stage(a.served.service, staged)  # noqa: SLF001
    assert stage["by"].startswith("p_")
    # another person who can see the ticket but may not release it: refused, nothing removed
    import types

    from orch.host.errors import HostError

    call = types.SimpleNamespace(
        column=types.SimpleNamespace(name="P"), viewer="p_someone_else", service=a.served.service, frame={}
    )
    asked = []

    def may_share(c, view):
        asked.append(c.viewer)
        raise HostError("role.denied", "sharing is for the ticket owner, owners and maintainers")

    mp = pytest.MonkeyPatch()
    mp.setattr(publishing, "_ticket", lambda c, ref: types.SimpleNamespace(key=key, uid=uid))
    mp.setattr(publishing, "_may_share", may_share)
    try:
        with pytest.raises(HostError) as caught:
            appops._discard(call, {"staged_id": staged})  # noqa: SLF001
    finally:
        mp.undo()
    assert caught.value.code == "role.denied" and asked == ["p_someone_else"]
    assert (a.served.lab.state / "hosts" / a.served.wid / "publish" / "appstage" / staged).exists()
    ok(a.req("app.discard", {"staged_id": staged}))  # the person who prepared it


def test_a_note_older_than_an_hour_is_no_approval(a):
    """F6: the signature is good for an hour (``APPROVAL_VALID``)."""
    uid, key = ticket(a)
    staged = ok(prepare(a, key, "index.html", "app.js"))["data"]["staged_id"]
    a.human.run("app.release", {"staged_id": staged})
    a.served.service.clock = (lambda c: lambda: c() + 3601)(a.served.service.clock)
    assert err(a.req("app.send", {"staged_id": staged})) == "publish.not_approved"


def test_a_viewer_cannot_send(a):
    """F6: ``role.denied`` for a person who may not release (here: no ticket owner, owner or maintainer role)."""
    from orch.host import appops

    uid, key = ticket(a)
    staged = ok(prepare(a, key, "index.html", "app.js"))["data"]["staged_id"]
    a.human.run("app.release", {"staged_id": staged})
    import orch.host.publishing as P

    def deny(call, view):
        raise appops.HostError("role.denied", "sharing is for the ticket owner, owners and maintainers")

    mp = pytest.MonkeyPatch()
    mp.setattr(P, "_may_share", deny)
    try:
        assert err(a.req("app.send", {"staged_id": staged})) == "role.denied"
    finally:
        mp.undo()
    assert [r for r in a.server.requests if "/apps/" in r["target"]] == []


# -- the security check of #1784: TOCTOU and self-reach ----------------------------------------------------------------


def _before_send(a, monkeypatch, action):
    """Run ``action`` after the plan reserved the version and before the upload: the window of the race."""
    from orch.host import appops

    real = appops._send  # noqa: SLF001

    def wrapped(call, plan):
        action(plan)
        return real(call, plan)

    monkeypatch.setattr(appops, "_send", wrapped)


def test_two_concurrent_sends_of_one_stage_upload_once_and_the_other_is_refused(a, monkeypatch):
    """The version is reserved from the plan until the record is saved: a second send meanwhile is refused at once."""
    uid, key = ticket(a)
    staged = ok(prepare(a, key, "index.html", "app.js"))["data"]["staged_id"]
    a.human.run("app.release", {"staged_id": staged})
    second = []
    _before_send(
        a, monkeypatch, lambda plan: second.append(err(a.served.client().req("app.send", {"staged_id": staged})))
    )
    ok(a.req("app.send", {"staged_id": staged}))
    assert second == ["lock.busy"]
    puts = [r for r in a.server.requests if r["method"] == "PUT" and r["target"].endswith("/versions/1")]
    assert len(puts) == 1
    ok(a.req("app.list", {}))  # the reservation is gone with the record saved


def test_a_stage_changed_between_plan_and_send_is_refused_and_nothing_is_uploaded(a, monkeypatch):
    uid, key = ticket(a)
    staged = ok(prepare(a, key, "index.html", "app.js"))["data"]["staged_id"]
    a.human.run("app.release", {"staged_id": staged})
    path = a.served.lab.state / "hosts" / a.served.wid / "publish" / "appstage" / staged / "files" / "app.js"
    _before_send(a, monkeypatch, lambda plan: path.write_bytes(bytes(len(APPJS))))  # same size, other bytes
    assert err(a.req("app.send", {"staged_id": staged})) == "publish.changed"
    assert [r for r in a.server.requests if "/apps/" in r["target"]] == []
    # the reservation was released: the stage can be sent after it is prepared and signed again
    again = ok(prepare(a, key, "index.html", "app.js", slug="sales-board-2"))["data"]["staged_id"]
    a.human.run("app.release", {"staged_id": again})
    ok(a.req("app.send", {"staged_id": again}))  # the hook only touched the first stage


def test_a_record_that_moved_between_plan_and_send_is_refused(a, monkeypatch):
    from orch.host import appops

    uid, key = ticket(a)
    first = ok(prepare(a, key, "index.html", "app.js"))["data"]
    a.human.run("app.release", {"staged_id": first["staged_id"]})
    rec = {"id": first["app_id"], "slug": "x", "ticket": uid, "versions": {"1": {}}, "live": None}
    _before_send(a, monkeypatch, lambda plan: appops._save_records(a.served.service, [rec]))  # noqa: SLF001
    assert err(a.req("app.send", {"staged_id": first["staged_id"]})) == "publish.changed"
    assert [r for r in a.server.requests if "/apps/" in r["target"]] == []


def test_an_app_may_not_reach_any_address_the_host_dials_for_the_hub(a, monkeypatch):
    """The forbidden set is the hub's whole dial list (``publish_route['addrs']``), not only its first address."""
    from orch.host import hubops

    uid, key = ticket(a)
    real = hubops.publish_route

    def route(service):
        got = real(service)
        return {**got, "addrs": ["hub-one.example.org:443", "hub-two.example.org:8443"]}

    monkeypatch.setattr(hubops, "publish_route", route)
    for origin in ("https://hub-one.example.org", "https://hub-two.example.org"):
        assert err(prepare(a, key, "index.html", "app.js", reach=[origin])) == "invalid.input", origin
    ok(prepare(a, key, "index.html", "app.js", reach=["https://api.example.com"]))


# -- going live: the second signature (#1455 go-live) ------------------------------------------------------------------


class LiveServer(AppServer):
    """AppServer that also takes the owner's approval on a commit, rollback or start, and checks it as orch-publish does
    (device key signature under the approval label, ``request_hash`` of THIS request, the body's access, the single-use
    nonce, the 120 s window)."""

    def __init__(self, wsk_pub, **kw):
        super().__init__(wsk_pub, **kw)
        self.apps = {}
        self.live = {}
        self.approvals = []
        self.seen_nonces = set()
        self.owner = None  # the Person whose approval counts

    def _authentic(self, headers, method, netloc, target, body):
        import base64

        from orch.canon.jcs import loads_strict
        from orch.hubclient.publish import approval_hash
        from tests.hub import contract as H

        h = {k.lower(): v for k, v in headers.items()}
        raw = h.get("orch-owner-approval")
        if raw is None:
            return super()._authentic(headers, method, netloc, target, body)
        ap = loads_strict(base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4)))
        signed = H.hub_path_signed_bytes(
            bytes.fromhex(h["orch-workspace"]), int(h["orch-ts"]), bytes.fromhex(h["orch-nonce"]), method, netloc,
            target, body, h["orch-hub"], ap,
        )  # fmt: skip
        assert approval_hash(ap) != bytes(32)
        from orch import crypto
        from orch.crypto import suite as S
        from tests.hub.vec import pub as vec_pub

        return (
            h["orch-hub"] == HUB_ID
            and S.verify(self.wsk_pub, crypto.unb64u(h["orch-signature"], 64), signed)
            and S.verify(
                vec_pub("hk_op"), crypto.unb64u(h["orch-hub-signature"], 64), H.hub_publish_bytes(HUB_ID, signed)
            )
        )

    def _serve(self, scheme, netloc, method, target, headers, body, timeout):
        import base64
        import re as _re

        from orch import crypto
        from orch.canon.jcs import dumps as _cj
        from orch.canon.jcs import loads_strict
        from orch.crypto import suite as S
        from orch.crypto.labels import L
        from orch.hubclient.publish import request_hash

        m = _re.fullmatch(r"/v1/ws/([0-9a-f]{32})/apps/([0-9a-f]{32})/(commit|rollback|start)", target)
        if method != "POST" or m is None:
            return super()._serve(scheme, netloc, method, target, headers, body, timeout)
        self.requests.append({"method": method, "target": target, "body": body, "netloc": netloc, "headers": headers})
        h = {k.lower(): v for k, v in headers.items()}
        if not self._authentic(headers, method, netloc, target, body):
            self.bad_signatures.append(f"{method} {target}")
            return 401, b'{"error":"bad_signature"}'
        raw = h.get("orch-owner-approval")
        if raw is None:
            return 403, b'{"error":"approval_required"}'
        ap = loads_strict(base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4)))
        nonce = h["orch-nonce"]
        if nonce in self.seen_nonces:
            return 403, b'{"error":"replay"}'
        self.seen_nonces.add(nonce)
        ws, ts = bytes.fromhex(h["orch-workspace"]), int(h["orch-ts"])
        o, cert = ap["o"], ap["device_cert"]["o"]
        want = crypto.b64u(request_hash(ws, ts, bytes.fromhex(nonce), method, netloc, target, body, HUB_ID))
        bad = (
            not S.verify(crypto.unb64u(cert["dk_sig_pub"], 65), crypto.unb64u(ap["sig"], 64),
                         L["sig_hub_publish_approval"] + _cj(o))
            or ap["pk_pub"] != crypto.b64u(self.owner.pk_pub)
            or o["request_hash"] != want
            or o["access"] != json.loads(body)["access"]
            or o["device_id"] != cert["device_id"]
        )  # fmt: skip
        if bad:
            return 403, b'{"error":"bad_approval"}'
        self.approvals.append(ap)
        self.live[m.group(2)] = (m.group(3), json.loads(body)["version"])
        return 200, b'{"result":"live"}'


class LiveHub(PublishHub):
    """The hub link as the host sees it, forwarding the owner's approval header too (hub ``publish/request.py``)."""

    def _forward(self, req):
        from orch import crypto
        from orch.crypto import suite as S
        from orch.hubclient.publish import approval_hash  # noqa: F401
        from tests.hub import contract as H
        from tests.hub.vec import private_key

        h = {k.lower(): v for k, v in req["headers"].items()}
        assert set(h) <= {"orch-workspace", "orch-ts", "orch-nonce", "orch-signature", "orch-owner-approval"}
        headers = dict(req["headers"]) | {"Orch-Hub": HUB_ID}
        approval = None
        if "orch-owner-approval" in h:
            import base64

            from orch.canon.jcs import loads_strict

            raw = h["orch-owner-approval"]
            approval = loads_strict(base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4)))
        signed = H.hub_path_signed_bytes(
            bytes.fromhex(h["orch-workspace"]), int(h["orch-ts"]), bytes.fromhex(h["orch-nonce"]), req["method"],
            req["host"], req["target"], bytes.fromhex(req["body_hex"]), HUB_ID, approval,
        )  # fmt: skip
        assert S.verify(self.server.wsk_pub, crypto.unb64u(h["orch-signature"], 64), signed), "host signature"
        headers["Orch-Hub-Signature"] = crypto.b64u(S.sign(private_key("hk_op"), H.hub_publish_bytes(HUB_ID, signed)))
        try:
            status, data = self.server(
                "https", req["host"], req["method"], req["target"], headers, bytes.fromhex(req["body_hex"]), 1.0
            )
        except OSError:
            return {}
        return {"status": status, "body_hex": data.hex()}


@pytest.fixture
def g(served, human, monkeypatch):
    """A world whose publish server takes approvals; the lab owner is the card owner."""
    world = World(served, human, monkeypatch)
    old = world.server
    srv = LiveServer(old.wsk_pub)
    srv.owner = world.served.lab.ws.owner
    world.hub = LiveHub(srv)
    monkeypatch.setattr(hubops, "TRANSPORT", lambda service, hub_id: world.hub)
    world.server = srv
    world.addon()
    world.configure()
    return world


def approve(g, commit, note, *, now_ms=None):
    """What the person's client does with the commit request of ``app.send``: check it against the note it signed and
    sign it with its device key (the CLI does the same with ``golive``)."""
    from orch import crypto
    from orch.publishclient import golive

    owner = g.served.lab.ws.owner
    device_id = crypto.device_id(owner.sig_pub).hex()
    req = {
        "target": commit["target"], "body": bytes.fromhex(commit["body_hex"]), "ts_ms": commit["ts_ms"],
        "nonce": bytes.fromhex(commit["nonce_hex"]), "host": commit["host"], "hub_id": commit["hub_id"],
    }  # fmt: skip
    o = golive.check_commit_request(
        req,
        commit["facts"],
        note,
        workspace_id=g.served.wid,
        hub_id=HUB_ID,
        device_id=device_id,
        now_ms=commit["ts_ms"] if now_ms is None else now_ms,
    )  # the hub is this test world's own (what the CLI reads from hub.json)
    cert = next(c for c in commit["device_certs"] if c["o"]["device_id"] == device_id)
    return golive.build_approval(o, owner.dk_sign, cert, commit["pk_pub"])


def release(g, key, *names, **args):
    data = ok(prepare(g, key, *names, **args))["data"]
    prepared, _, _ = g.human.run("app.release", {"staged_id": data["staged_id"]})
    return data, prepared["events"][0]["text"]


def test_the_owner_signs_twice_in_one_click_and_the_app_goes_live(g):
    uid, key = ticket(g)
    data, note = release(g, key, "index.html", "app.js")
    sent = ok(g.req("app.send", {"staged_id": data["staged_id"]}))["data"]
    assert sent["uploaded"] is True and sent["live"] is False and "commit" in sent
    commit = sent["commit"]
    ap = approve(g, commit, note)
    done = ok(g.req("app.commit", {"request_id": commit["request_id"], "approval": ap}))["data"]
    assert done == {"app_id": data["app_id"], "version": 1, "op": "commit", "live": True}
    assert g.server.live == {data["app_id"]: ("commit", 1)} and g.server.bad_signatures == []
    rows = ok(g.req("app.list", {}))["data"]["apps"]
    assert rows[0]["live"] == 1


def test_the_request_is_used_once_and_expires(g):
    uid, key = ticket(g)
    data, note = release(g, key, "index.html", "app.js")
    commit = ok(g.req("app.send", {"staged_id": data["staged_id"]}))["data"]["commit"]
    ap = approve(g, commit, note)
    ok(g.req("app.commit", {"request_id": commit["request_id"], "approval": ap}))
    assert err(g.req("app.commit", {"request_id": commit["request_id"], "approval": ap})) == "publish.changed"
    # an expired request is gone too
    data2, note2 = release(g, key, "index.html", "app.js", app_id=data["app_id"], name="Sales board v2")
    commit2 = ok(g.req("app.send", {"staged_id": data2["staged_id"]}))["data"]["commit"]
    ap2 = approve(g, commit2, note2)
    g.served.service.clock = (lambda c: lambda: c() + 91)(g.served.service.clock)
    assert err(g.req("app.commit", {"request_id": commit2["request_id"], "approval": ap2})) == "publish.changed"
    assert data["app_id"] not in {k for k, v in g.server.live.items() if v[1] == 2}


def test_an_approval_for_another_request_is_refused_before_it_is_sent(g):
    uid, key = ticket(g)
    data, note = release(g, key, "index.html", "app.js")
    first = ok(g.req("app.send", {"staged_id": data["staged_id"]}))["data"]["commit"]
    stale = approve(g, first, note)
    again = ok(g.req("app.commit_request", {"app_id": data["app_id"], "op": "commit", "version": 1}))["data"]
    assert again["request_id"] != first["request_id"] and again["nonce_hex"] != first["nonce_hex"]
    before = len([r for r in g.server.requests if r["target"].endswith("/commit")])
    res = g.req("app.commit", {"request_id": again["request_id"], "approval": stale})  # the approval of the first draw
    assert err(res) in {"publish.server_refused", "publish.changed"}
    assert len([r for r in g.server.requests if r["target"].endswith("/commit")]) == before  # nothing was sent
    # the failed attempt used the request up; a fresh one, approved for itself, goes through
    third = ok(g.req("app.commit_request", {"app_id": data["app_id"], "op": "commit", "version": 1}))["data"]
    ok(g.req("app.commit", {"request_id": third["request_id"], "approval": approve(g, third, note)}))
    assert g.server.live == {data["app_id"]: ("commit", 1)}


def test_nothing_goes_live_for_an_agent_or_a_non_owner(g):
    uid, key = ticket(g)
    data, note = release(g, key, "index.html", "app.js")
    sent = ok(g.req("app.send", {"staged_id": data["staged_id"]}))["data"]
    commit = sent["commit"]
    for op, args in (
        ("app.commit", {"request_id": commit["request_id"], "approval": {}}),
        ("app.commit_request", {"app_id": data["app_id"], "op": "commit", "version": 1}),
    ):
        assert err(g.req(op, args, agent=True)) == "op.not_allowed", op
    assert g.server.live == {}


def test_a_commit_request_needs_a_fresh_signature_and_a_rollback_goes_through_a_fileless_stage(g):
    uid, key = ticket(g)
    one, note1 = release(g, key, "index.html", "app.js")
    c1 = ok(g.req("app.send", {"staged_id": one["staged_id"]}))["data"]["commit"]
    ok(g.req("app.commit", {"request_id": c1["request_id"], "approval": approve(g, c1, note1)}))
    two, note2 = release(g, key, "index.html", "app.js", app_id=one["app_id"], name="Sales board 2")
    c2 = ok(g.req("app.send", {"staged_id": two["staged_id"]}))["data"]["commit"]
    ok(g.req("app.commit", {"request_id": c2["request_id"], "approval": approve(g, c2, note2)}))
    assert g.server.live[one["app_id"]] == ("commit", 2)
    # go back to version 1: a stage with no files, signed like any release, then only the commit request
    back = ok(g.req("app.prepare", {"ref": key, "app_id": one["app_id"], "op": "rollback", "version": 1}))["data"]
    assert back["files"] == [] and back["op"] == "rollback"
    prepared, _, _ = g.human.run("app.release", {"staged_id": back["staged_id"]})
    note3 = prepared["events"][0]["text"]
    assert "Action: go back to this older version" in note3 and "Compared with the live version 2:" in note3
    sent = ok(g.req("app.send", {"staged_id": back["staged_id"]}))["data"]
    assert sent["uploaded"] is False and sent["op"] == "rollback"
    ok(g.req("app.commit", {"request_id": sent["commit"]["request_id"], "approval": approve(g, sent["commit"], note3)}))
    assert g.server.live[one["app_id"]] == ("rollback", 1)
    # a rollback to a version that is not below the live one, and a start of another version, are refused up front
    assert (
        err(g.req("app.prepare", {"ref": key, "app_id": one["app_id"], "op": "rollback", "version": 1}))
        == "invalid.input"
    )
    assert (
        err(g.req("app.prepare", {"ref": key, "app_id": one["app_id"], "op": "start", "version": 2})) == "invalid.input"
    )


# -- Opus review of #1788 ----------------------------------------------------------------------------------------------


def test_a_version_that_was_never_live_cannot_be_gone_back_to(g):
    """F1: ``versions`` holds every UPLOADED version; only one the owner made live can come back (``was_live``)."""
    uid, key = ticket(g)
    one, note1 = release(g, key, "index.html", "app.js")
    ok(g.req("app.send", {"staged_id": one["staged_id"]}))  # uploaded, never committed
    two, note2 = release(g, key, "index.html", "app.js", app_id=one["app_id"], name="Sales board 2")
    c2 = ok(g.req("app.send", {"staged_id": two["staged_id"]}))["data"]["commit"]
    ok(g.req("app.commit", {"request_id": c2["request_id"], "approval": approve(g, c2, note2)}))
    assert g.server.live[one["app_id"]] == ("commit", 2)
    res = g.req("app.prepare", {"ref": key, "app_id": one["app_id"], "op": "rollback", "version": 1})
    assert err(res) == "invalid.input"  # version 1 was uploaded but never made live
    three, note3 = release(g, key, "index.html", "app.js", app_id=one["app_id"], name="Sales board 3")
    c3 = ok(g.req("app.send", {"staged_id": three["staged_id"]}))["data"]["commit"]
    ok(g.req("app.commit", {"request_id": c3["request_id"], "approval": approve(g, c3, note3)}))
    ok(g.req("app.prepare", {"ref": key, "app_id": one["app_id"], "op": "rollback", "version": 2}))  # was live: fine


def test_a_checked_object_is_the_only_thing_that_gets_signed():
    """N1: ``build_approval`` refuses a plain dict, however it was made."""
    from orch.hubclient.errors import HubRefused
    from orch.publishclient import golive

    with pytest.raises(HubRefused):
        golive.build_approval({"kind": "hub.publish_approval"}, lambda payload: bytes(64), {}, "pk")
    assert not hasattr(golive, "approval_object")  # no public way to make the object


def test_the_approval_must_have_the_shape_of_one(g):
    """N4: exactly o, sig, device_cert and pk_pub, bounded, before anything is planned."""
    uid, key = ticket(g)
    data, note = release(g, key, "index.html", "app.js")
    commit = ok(g.req("app.send", {"staged_id": data["staged_id"]}))["data"]["commit"]
    good = approve(g, commit, note)
    for bad in (
        {}, {"o": {}}, {**good, "extra": 1}, {**good, "sig": "x"}, {**good, "pk_pub": 5},
        {**good, "device_cert": {**good["device_cert"], "pad": "x" * 9000}},
    ):  # fmt: skip
        assert err(g.req("app.commit", {"request_id": commit["request_id"], "approval": bad})) == "invalid.input"
    assert g.server.live == {}
    ok(g.req("app.commit", {"request_id": commit["request_id"], "approval": good}))  # none of it used the request


def test_a_maintainer_uploads_but_cannot_make_it_live(g, monkeypatch):
    """N3: a person who is not the card owner gets no commit request, and ``commit_request`` and ``commit`` are
    ``role.denied``."""
    from orch.host import appops

    uid, key = ticket(g)
    data, note = release(g, key, "index.html", "app.js")
    monkeypatch.setattr(appops, "_owner_person", lambda service: "p_someone_else")  # the caller is not the card owner
    sent = ok(g.req("app.send", {"staged_id": data["staged_id"]}))["data"]
    assert sent["uploaded"] is True and "commit" not in sent and "owner" in sent["needs"]
    assert err(g.req("app.commit_request", {"app_id": data["app_id"], "op": "commit", "version": 1})) == "role.denied"
    assert err(g.req("app.commit", {"request_id": "cr_" + "0" * 32, "approval": approve_stub()})) in {
        "role.denied",
        "publish.changed",
    }  # no pending request exists for them at all
    assert g.server.live == {}


def approve_stub():
    return {"o": {}, "sig": "s" * 86, "device_cert": {}, "pk_pub": "p" * 87}


def test_a_commit_request_after_the_note_is_an_hour_old_is_not_approved(g):
    """N3: the signature is good for an hour; ``commit_request`` asks for a fresh one."""
    uid, key = ticket(g)
    data, note = release(g, key, "index.html", "app.js")
    ok(g.req("app.send", {"staged_id": data["staged_id"]}))
    g.served.service.clock = (lambda c: lambda: c() + 3601)(g.served.service.clock)
    res = g.req("app.commit_request", {"app_id": data["app_id"], "op": "commit", "version": 1})
    assert err(res) == "publish.not_approved"
