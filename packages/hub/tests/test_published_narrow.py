"""M4 S2 (#1453, protocol.md 11.6): the hub's two narrowing buttons. ``NarrowClient`` builds and signs ``hub.release_narrow``
(only revoke for a link, stop for an app) and sends it to the publish origin it was configured with; the web API finds the
row in publish's own admin read (never from the browser), refuses everything but a full-mode workspace's row, and writes
the activity record. Nothing here can widen: there is no extend, password, start or approval route."""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest
from webapi_helpers import Web

from orch_hub.activity.store import ActivityLog
from orch_hub.contract import hub as H
from orch_hub.contract import keystore as KS
from orch_hub.contract import published as PUB
from orch_hub.contract import vectorgen as G
from orch_hub.contract.canon import b64u
from orch_hub.publish import NarrowClient, NarrowError
from orch_hub.webapi import features
from orch_hub.webapi.api import ROUTES

NOW = G.NOW
SHARE, APP = "ab" * 16, "cd" * 16
WS_FULL, WS_SEL = "11" * 16, "22" * 16


# --- the client ------------------------------------------------------------------------------------------------


class Nonces:
    def __init__(self):
        self.seen = set()

    def record(self, hub, nonce, now_ms):
        if (hub, nonce) in self.seen:
            return False
        self.seen.add((hub, nonce))
        return True


class FakePublish:
    """PUT /v2/hub/narrow, verified with the contract's own check (the oracle)."""

    def __init__(self, mode="full", answer=None, status=200):
        self.seen, self.nonces, self.answer, self.status = [], Nonces(), answer, status
        outer = self
        cards = {G.WS_A: {"wsk_pub": G.K.b("wsk_a")}}
        b = H.verify_binding(
            G.binding(mode=mode), workspace_id=G.WS_A, wsk_pub=G.K.pub["wsk_a"], now_ms=NOW, dev_mode=True
        )
        self.args = {"cards": cards, "bindings": {G.WS_A: H.apply_binding(None, b)}}

        class H_(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_PUT(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                outer.seen.append((self.path, body, dict(self.headers)))
                if outer.answer is not None:
                    return self._send(outer.answer, outer.status)
                held = H.verify_card(G.card(), now_ms=NOW, dev_mode=True)
                try:
                    res = PUB.check_release_narrow(
                        body,
                        held=held,
                        nonces=outer.nonces,
                        now_ms=NOW,
                        owners={f"share:{SHARE}": G.WS_A, f"app:{APP}": G.WS_A},
                        **outer.args,
                    )
                except H.Refused as e:
                    return self._send({"error": e.code}, 403)
                self._send(
                    {
                        "ok": True,
                        "target": res["target"],
                        "id": res["id"],
                        "result": "revoked" if res["target"] == "share" else "stopped",
                    }
                )

            def _send(self, obj, status=200):
                data = json.dumps(obj).encode()
                self.send_response(status)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self.srv = HTTPServer(("127.0.0.1", 0), H_)
        self.url = f"http://127.0.0.1:{self.srv.server_port}"
        threading.Thread(target=lambda: self.srv.serve_forever(0.02), daemon=True).start()

    def close(self):
        self.srv.shutdown()
        self.srv.server_close()


@pytest.fixture
def fake():
    made = []

    def make(**kw):
        f = FakePublish(**kw)
        made.append(f)
        return f

    yield make
    for f in made:
        f.close()


def client(url, nonce=None, signer=None):
    return NarrowClient(url, signer or KS.OpSigner(G.K.priv["hk_op"]), G.HUB_ID, now_ms=lambda: NOW, nonce=nonce)


def test_a_link_is_revoked_and_an_app_stopped_with_a_signature_the_contract_accepts(fake):
    f = fake()
    c = client(f.url)
    assert c.narrow(G.WS_A, "share", SHARE) == "revoked"
    assert c.narrow(G.WS_A, "app", APP) == "stopped"
    assert [p for p, _, _ in f.seen] == ["/v2/hub/narrow"] * 2
    o = f.seen[0][1]["o"]
    assert (o["kind"], o["action"], o["target"], o["hub_id"], o["workspace_id"]) == (
        "hub.release_narrow",
        "revoke",
        "share",
        G.HUB_ID,
        G.WS_A,
    )
    assert f.seen[1][1]["o"]["action"] == "stop"
    assert f.seen[0][1]["o"]["nonce"] != f.seen[1][1]["o"]["nonce"]  # a fresh nonce each time


def test_only_revoke_for_a_link_and_stop_for_an_app_can_ever_be_built():
    c = client("http://127.0.0.1:1")
    for target in ("start", "extend", "remove", "pass", "", None, "shares"):
        with pytest.raises(NarrowError):
            c.build(G.WS_A, target, SHARE)
    for ws, i in (("zz", SHARE), (G.WS_A, "../x"), (G.WS_A, SHARE.upper()), (G.WS_A, SHARE + "&x=1")):
        with pytest.raises(NarrowError):
            c.build(ws, "share", i)
    # the signer itself refuses to sign what is not a hub kind of this key
    with pytest.raises(KS.KeyUseError):
        KS.OpSigner(G.K.priv["hk_op"]).sign_object({"v": 2, "suite": 2, "hv": 0, "kind": "hub.publish_approval"})


def test_the_request_goes_only_to_the_configured_origin(fake):
    f = fake()
    other = fake()
    client(f.url).narrow(G.WS_A, "share", SHARE)
    assert len(f.seen) == 1 and other.seen == []
    for bad in ("http://example.com", "ftp://127.0.0.1", "http://user:pw@127.0.0.1", "http://127.0.0.1/path"):
        with pytest.raises(ValueError):
            client(bad)


def test_a_wrong_signer_is_refused_and_a_replay_is_refused(fake):
    f = fake()
    with pytest.raises(NarrowError) as e:
        client(f.url, signer=KS.OpSigner(G.K.priv["hk_op_rogue"])).narrow(G.WS_A, "share", SHARE)
    assert e.value.code == "unavailable" and e.value.raw == "bad_hub_signature"
    fixed = client(f.url, nonce=lambda: b"\x07" * 32)
    fixed.narrow(G.WS_A, "share", SHARE)
    with pytest.raises(NarrowError) as e:
        fixed.narrow(G.WS_A, "share", SHARE)
    assert e.value.raw == "replay"


def test_a_selective_workspace_takes_no_narrowing(fake):
    f = fake(mode="selective")
    with pytest.raises(NarrowError) as e:
        client(f.url).narrow(G.WS_A, "share", SHARE)
    assert e.value.code == "not_bound"


def test_refusal_codes_are_collapsed_for_the_page(fake):
    for raw, shown in (("not_found", "unknown_item"), ("other_workspace", "unknown_item"), ("not_bound", "not_bound"),
                       ("other_hub", "not_bound"), ("stale_timestamp", "unavailable"), ("dev_refused", "unavailable")):  # fmt: skip
        f = fake(answer={"error": raw}, status=403)
        with pytest.raises(NarrowError) as e:
            client(f.url).narrow(G.WS_A, "share", SHARE)
        assert (e.value.code, e.value.raw) == (shown, raw)


@pytest.mark.parametrize(
    "answer",
    [
        {"ok": True, "target": "share", "id": SHARE, "result": "revoked", "x": 1},
        {"ok": True, "target": "app", "id": SHARE, "result": "revoked"},  # another target than asked
        {"ok": True, "target": "share", "id": "ff" * 16, "result": "revoked"},
        {"ok": True, "target": "share", "id": SHARE, "result": "extended"},
        {"ok": False, "target": "share", "id": SHARE, "result": "revoked"},
        [1],
        "text",
    ],
)
def test_an_answer_that_breaks_the_shape_is_unavailable(fake, answer):
    f = fake(answer=answer)
    with pytest.raises(NarrowError) as e:
        client(f.url).narrow(G.WS_A, "share", SHARE)
    assert e.value.code == "unavailable"


def test_publish_down_is_unavailable():
    with pytest.raises(NarrowError) as e:
        client("http://127.0.0.1:1").narrow(G.WS_A, "share", SHARE)
    assert e.value.code == "unavailable"


# --- the web API -----------------------------------------------------------------------------------------------


def link(**kw):
    row = {
        "workspace_id": WS_FULL, "id": SHARE, "type": "link", "kind": "secret", "state": "live", "created_ms": NOW - 1000,
        "expires_ms": NOW + 86_400_000, "bytes": 12, "label": "Q3 report", "ticket": None, "by_kind": "person",
        "by_name": "Ana", "views": 3, "has_password": False,
    }  # fmt: skip
    row.update(kw)
    return row


def app_row(**kw):
    row = {
        "workspace_id": WS_FULL, "id": APP, "type": "app", "kind": "app", "state": "running", "created_ms": NOW - 1000,
        "updated_ms": NOW - 500, "bytes": 99, "label": "Shop", "ticket": None, "by_kind": "person", "by_name": "Ana",
        "has_password": False, "slug": "shop-demo", "name": "Shop", "access": "public", "version": 2, "visits_30d": 5,
    }  # fmt: skip
    row.update(kw)
    return row


class Reader:
    def __init__(self, rows):
        self.rows = rows

    def published(self, workspace=None):
        return self.rows


class Narrower:
    def __init__(self, result="revoked", error=None):
        self.calls, self.result, self.error = [], result, error

    def narrow(self, workspace_id, target, item_id):
        self.calls.append((workspace_id, target, item_id))
        if self.error:
            raise NarrowError(self.error)
        return self.result


@pytest.fixture
def w(tmp_path):
    x = Web(tmp_path)
    x.reg.add(WS_FULL, "full", name="Product")
    x.reg.add(WS_SEL, "selective", name="Marketing")
    x.api.d.activity = ActivityLog()
    yield x
    x.close()


def post(w, path, tok=None, **kw):
    return w.call("POST", path, {}, token=tok or w.login(w.admin), **kw)


def events(w):
    return [
        (e["type"], e["workspace_id"], e["detail"])
        for e in w.api.d.activity.list()
        if e["type"] in ("link.revoked", "app.stopped")
    ]


def test_routes_are_admin_only_posts_and_there_is_no_widening_route(w):
    w.api.d.published, w.api.d.narrower = Reader([link()]), Narrower()
    paths = f"/api/v1/admin/published/links/{SHARE}/revoke"
    assert w.call("POST", f"/admin/published/links/{SHARE}/revoke", {})[0] == 401
    assert post(w, f"/admin/published/links/{SHARE}/revoke", w.login(w.ann))[0] == 403
    assert post(w, f"/admin/published/links/{SHARE}/revoke", w.login(w.admin, "app"))[0] in (401, 403)
    assert paths  # the route table below is the whole of it
    mine = [(r.method, r.path) for r in ROUTES if "/admin/published" in r.path]
    assert sorted(mine) == [
        ("GET", "/api/v1/admin/published"),
        ("POST", "/api/v1/admin/published/apps/{app_id}/stop"),
        ("POST", "/api/v1/admin/published/links/{share_id}/revoke"),
    ]
    assert any(f.mixin.__name__ == "PublishedHandlers" for f in features.FEATURES)


def test_revoke_a_link_goes_through_the_narrower_and_is_recorded(w):
    n = Narrower()
    w.api.d.published, w.api.d.narrower = Reader([link(), app_row()]), n
    s, o, _ = post(w, f"/admin/published/links/{SHARE}/revoke")
    assert (s, o) == (200, {"id": SHARE, "state": "revoked", "result": "revoked"})
    assert n.calls == [(WS_FULL, "share", SHARE)]  # the workspace comes from publish's row, not from the browser
    assert events(w) == [("link.revoked", WS_FULL, {"link": SHARE})]


def test_stop_an_app(w):
    n = Narrower(result="stopped")
    w.api.d.published, w.api.d.narrower = Reader([link(), app_row()]), n
    s, o, _ = post(w, f"/admin/published/apps/{APP}/stop")
    assert (s, o) == (200, {"id": APP, "state": "stopped", "result": "stopped"})
    assert n.calls == [(WS_FULL, "app", APP)]
    assert events(w) == [("app.stopped", WS_FULL, {"app": APP})]


def test_the_browser_cannot_name_a_workspace_or_an_action(w):
    n = Narrower()
    w.api.d.published, w.api.d.narrower = Reader([link()]), n
    tok = w.login(w.admin)
    # a body is ignored: the workspace comes from the row publish listed, and the action from the route
    s, _o, _ = w.call(
        "POST",
        f"/admin/published/links/{SHARE}/revoke",
        {"workspace_id": WS_SEL, "action": "start", "target": "app"},
        token=tok,
    )
    assert s == 200 and n.calls == [(WS_FULL, "share", SHARE)]


def test_unknown_selective_and_wrong_kind_ids_are_refused_before_anything_is_signed(w):
    n = Narrower()
    sel = link(id="ee" * 16, workspace_id=WS_SEL)
    w.api.d.published, w.api.d.narrower = Reader([link(), app_row(), sel]), n
    tok = w.login(w.admin)
    for path in (
        f"/admin/published/links/{'99' * 16}/revoke",  # not listed
        f"/admin/published/links/{'ee' * 16}/revoke",  # a selective workspace's row is dropped
        f"/admin/published/links/{APP}/revoke",  # an app id on the link route
        f"/admin/published/apps/{SHARE}/stop",  # a link id on the app route
        "/admin/published/links/not-hex/revoke",
        f"/admin/published/links/{SHARE.upper()}/revoke",
    ):
        s, o, _ = w.call("POST", path, {}, token=tok)
        assert (s, o["error"]) in ((404, "unknown_item"), (404, "unknown_op")), path
    assert n.calls == []


def test_a_workspace_that_left_full_mode_is_refused(w):
    n = Narrower()
    w.api.d.published, w.api.d.narrower = Reader([link()]), n
    w.reg.add(WS_FULL, "selective", name="Product")
    assert post(w, f"/admin/published/links/{SHARE}/revoke")[0] == 404
    assert n.calls == []


def test_narrower_failures_show_as_their_codes(w):
    w.api.d.published = Reader([link()])
    for err, status in (("not_bound", 409), ("unknown_item", 404), ("unavailable", 503)):
        w.api.d.narrower = Narrower(error=err)
        s, o, _ = post(w, f"/admin/published/links/{SHARE}/revoke")
        assert (s, o["error"]) == (status, err)


def test_publish_down_for_the_read_is_unavailable(w):
    from orch_hub.publish import AdminReadError

    class Down:
        def published(self, workspace=None):
            raise AdminReadError("unavailable")

    n = Narrower()
    w.api.d.published, w.api.d.narrower = Down(), n
    assert post(w, f"/admin/published/links/{SHARE}/revoke")[0] == 503
    assert n.calls == []


def test_an_unchanged_result_records_nothing_new(w):
    w.api.d.published, w.api.d.narrower = Reader([link(state="revoked")]), Narrower(result="unchanged")
    s, o, _ = post(w, f"/admin/published/links/{SHARE}/revoke")
    assert (s, o["result"]) == (200, "unchanged")
    assert events(w) == []


def test_the_hub_signs_only_what_the_contract_names():
    # the hub's one narrowing action table is the contract's
    assert PUB.NARROW_ACTION == {"share": "revoke", "app": "stop"}
    assert b64u(b"x")  # (import used by the shape tests above)
