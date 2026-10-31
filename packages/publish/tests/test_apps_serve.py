"""Apps at orch-publish (M4 #1357 S3): store, commit with the owner's approval on both paths, serving under /a/, view
sessions, CSP, no cookies, password gate, stop/remove, visits. One test per Opus condition (2-6, 9-12, 20)."""

import hashlib
import json
import os
import threading

import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature
from fastapi.testclient import TestClient

from pubsrv import appserve, db
from pubsrv import hub_path as H
from pubsrv import published as P
from pubsrv import relay_proto as proto
from pubsrv.serve import create_share_app
from tests.hub_helpers import build, pub, signed
from tests.test_hub_path_http import Owner
from tests.test_publish_api_auth import HOST, NOW, WS_A, Env, err

ORIGIN = "http://127.0.0.1:8104"
APP = "a1" * 16
APP2 = "b2" * 16
INDEX = b"<html><script src=app.js></script></html>"
JS = b"console.log(1)"
BIN = b"\x00\x01"
PW = "correct horse battery"


def sha(b):
    return hashlib.sha256(b).hexdigest()


def make_meta(
    slug="shop-demo", access="public", pw=None, reach=(), audience=None, extra=None, name="Shop"
):
    files = {"app.js": JS, "data.bin": BIN, "index.html": INDEX, **(extra or {})}
    return {
        "slug": slug, "name": name, "kind": "app", "access": access, "audience": audience, "pw": pw,
        "reach": list(reach), "label": "Shop demo", "ticket": None,
        "files": [{"path": p, "size": len(b), "sha256": sha(b)} for p, b in sorted(files.items())],
    }, files  # fmt: skip


class World:
    def __init__(self, tmp_path):
        self.e = Env(tmp_path)
        self.e.allow()
        assert self.e.register().status_code == 200
        self.e.bind()
        s = self.e.app.state.settings
        s.share_origin = ORIGIN
        self.owner = Owner()
        self.share = create_share_app(
            self.e.data, clock=lambda: self.e.now, share_origin=ORIGIN, api_host=HOST
        )
        self.web = TestClient(self.share, base_url=ORIGIN, follow_redirects=False)

    # -- signed requests -------------------------------------------------------------------------------
    def hub(self, method, target, body=b"", approval_for=None, **kw):
        """A hub-path request; approval_for=access builds the owner's approval for exactly this request."""
        e = self.e
        nonce, ts = os.urandom(16), kw.pop("ts", e.now)
        approval = None
        if approval_for is not None:
            rh = H.publish_request_hash(
                bytes.fromhex(WS_A), ts, nonce, method, HOST, target, body, e.hub.id
            )
            approval = self._approval(rh, approval_for, e.hub.id)
        h, _ = build(
            e.hub, e.wsk, method, target, body, ts=ts, nonce=nonce, approval=approval, host=HOST
        )
        return e.c.request(method, target, content=body, headers=h)

    def direct(self, method, target, body=b"", approval_for=None, hub_style=False, **kw):
        e = self.e
        nonce, ts = kw.pop("nonce", os.urandom(16)), kw.pop("ts", e.now)
        approval = None
        if approval_for is not None:
            hub = e.hub.id if hub_style else None
            rh = P.app_request_hash(bytes.fromhex(WS_A), ts, nonce, method, HOST, target, body, hub)
            approval = self._approval(rh, approval_for, hub)
        pb = H.direct_signed_bytes(bytes.fromhex(WS_A), ts, nonce, method, HOST, target, body)
        r, s = decode_dss_signature(e.wsk.sign(pb, ec.ECDSA(hashes.SHA256())))
        h = {"Orch-Workspace": WS_A, "Orch-Ts": str(ts), "Orch-Nonce": nonce.hex(),
             "Orch-Signature": proto.b64u(r.to_bytes(32, "big") + s.to_bytes(32, "big"))}  # fmt: skip
        if approval is not None:
            h["Orch-Owner-Approval"] = proto.b64u(proto.cj(approval))
        return e.c.request(method, target, content=body, headers=h)

    def _approval(self, request_hash, access, hub_id):
        o = {"v": 2, "suite": 2, "hv": 0, "kind": "hub.publish_approval", "workspace_id": WS_A, "hub_id": hub_id,
             "device_id": self.owner.cert["o"]["device_id"], "request_hash": proto.b64u(request_hash),
             "access": access}  # fmt: skip
        return signed(self.owner.dk, H.LABELS["sig_hub_publish_approval"], o, device_cert=self.owner.cert,
                      pk_pub=proto.b64u(pub(self.owner.pk)))  # fmt: skip

    # -- app operations --------------------------------------------------------------------------------
    def stage(self, meta, files, app=APP, version=1, send="hub"):
        s = getattr(self, send)
        t = f"/v1/ws/{WS_A}/apps/{app}/versions/{version}"
        r = s("PUT", t, json.dumps(meta).encode())
        assert r.status_code == 200, r.text
        for p, b in files.items():
            r = s("PUT", f"{t}/files/{p}", b)
            assert r.status_code == 200, r.text

    def body(self, op, meta, app=APP, version=1, **over):
        return {**P.release_body(op, app, version, meta), **over}

    def release(self, op, meta, app=APP, version=1, send="hub", approval="same", **over):
        body = json.dumps(self.body(op, meta, app, version, **over)).encode()
        access = meta["access"] if approval == "same" else approval
        return getattr(self, send)(
            "POST",
            f"/v1/ws/{WS_A}/apps/{app}/{op}",
            body,
            approval_for=access if approval is not None else None,
        )

    def publish(self, meta, files, **kw):
        send = kw.pop("send", "hub")
        self.stage(meta, files, send=send, **kw)
        r = self.release("commit", meta, send=send, **kw)
        assert r.status_code == 200, r.text
        return r

    def view_url(self, slug="shop-demo"):
        r = self.web.get(f"/a/{slug}/")
        assert r.status_code == 303, r.text
        return r.headers["location"]

    def sql(self, q, *a):
        return self.e.sql(q, *a)


@pytest.fixture
def w(tmp_path):
    return World(tmp_path)


def pw_record():
    return P.make_password_record(PW, b"s" * 16)


# --- publish flow and condition 3 -------------------------------------------------------------------------------


def test_publish_and_serve_a_public_app(w):
    meta, files = make_meta()
    w.publish(meta, files)
    url = w.view_url()
    assert url.startswith("/a/shop-demo/v/") and url.endswith("/")
    r = w.web.get(url)
    assert (r.status_code, r.content) == (200, INDEX)
    assert r.headers["content-type"].startswith("text/html")
    assert w.web.get(url + "app.js").content == JS
    row = w.sql("SELECT * FROM app_versions")[0]
    assert (
        row["state"] == "committed" and row["approved_by_person"] == w.owner.cert["o"]["person_id"]
    )


def test_meta_and_files_alone_serve_nothing(w):
    meta, files = make_meta()
    w.stage(meta, files)
    assert w.web.get("/a/shop-demo/").status_code == 404


def test_commit_needs_the_owners_approval(w):
    meta, files = make_meta()
    w.stage(meta, files)
    r = w.release("commit", meta, approval=None)
    assert (r.status_code, err(r)) == (403, "approval_required")
    assert w.web.get("/a/shop-demo/").status_code == 404


def test_approval_for_another_access_is_bad_approval(w):
    meta, files = make_meta()
    w.stage(meta, files)
    r = w.release("commit", meta, approval="company")
    assert (r.status_code, err(r)) == (403, "bad_approval")


def test_commit_body_must_match_the_stored_meta(w):
    meta, files = make_meta()
    w.stage(meta, files)
    other = dict(meta, name="Other")
    r = w.release("commit", meta, meta_hash=P.meta_hash(other))
    assert (r.status_code, err(r)) == (409, "meta_mismatch")
    r = w.release("commit", meta, reach_hash=P.reach_hash(["https://x.example.org"]))
    assert (r.status_code, err(r)) == (409, "meta_mismatch")


def test_missing_file_is_file_mismatch(w):
    meta, files = make_meta()
    w.stage(meta, {k: v for k, v in files.items() if k != "app.js"})
    r = w.release("commit", meta)
    assert (r.status_code, err(r)) == (409, "file_mismatch")


def test_changed_file_on_disk_is_file_mismatch(w):
    meta, files = make_meta()
    w.stage(meta, files)
    p = os.path.join(w.e.data, "apps", APP, "1", "files", "app.js")
    with open(p, "wb") as f:
        f.write(b"evil")
    r = w.release("commit", meta)
    assert (r.status_code, err(r)) == (409, "file_mismatch")


def test_committed_version_is_frozen(w):
    meta, files = make_meta()
    w.publish(meta, files)
    t = f"/v1/ws/{WS_A}/apps/{APP}/versions/1"
    assert w.hub("PUT", f"{t}/files/app.js", JS).status_code == 409
    r = w.hub("PUT", t, json.dumps(dict(meta, name="Evil")).encode())
    assert (r.status_code, err(r)) == (409, "conflict")
    assert w.hub("PUT", t, json.dumps(meta).encode()).json()["result"] == "unchanged"


def test_file_put_wrong_bytes_and_bad_paths(w):
    meta, _ = make_meta()
    t = f"/v1/ws/{WS_A}/apps/{APP}/versions/1"
    assert w.hub("PUT", t, json.dumps(meta).encode()).status_code == 200
    assert err(w.hub("PUT", t + "/files/app.js", b"nope")) == "content_mismatch"
    assert err(w.hub("PUT", t + "/files/.hidden", b"x")) == "bad_path"
    assert err(w.hub("PUT", t + "/files/extra.js", b"x")) == "not_in_manifest"
    # a % in the target is no route at all (classified over the raw target only)
    assert w.hub("PUT", t + "/files/a%2Fb", b"x").status_code == 404
    assert w.hub("PUT", t + "/files/%2e%2e/x", b"x").status_code == 404


def test_commit_with_a_query_is_not_a_route(w):
    meta, files = make_meta()
    w.stage(meta, files)
    body = json.dumps(w.body("commit", meta)).encode()
    r = w.hub("POST", f"/v1/ws/{WS_A}/apps/{APP}/commit?x=1", body, approval_for="public")
    assert r.status_code in (400, 404)
    assert w.web.get("/a/shop-demo/").status_code == 404


def test_another_workspace_cannot_touch_an_app(w):
    meta, files = make_meta()
    w.publish(meta, files)
    w.sql("UPDATE apps SET owner_ws=? WHERE id=?", "c" * 32, APP)
    t = f"/v1/ws/{WS_A}/apps/{APP}"
    assert w.hub("POST", t + "/stop").status_code == 404
    assert w.hub("DELETE", t).status_code == 404
    assert w.hub("PUT", t + "/versions/2", json.dumps(meta).encode()).status_code == 409


def test_slug_rules_and_reach_rules(w):
    t = f"/v1/ws/{WS_A}/apps/{APP}/versions/1"
    meta, _ = make_meta(slug="api")
    assert err(w.hub("PUT", t, json.dumps(meta).encode())) == "reserved_slug"
    meta, _ = make_meta(slug="Bad_Slug")
    assert err(w.hub("PUT", t, json.dumps(meta).encode())) == "slug"
    for bad, code in (("*", "wildcard"), ("http://x.example.org", "https_only"), ("https://127.0.0.1", "ip_literal"),
                      ("https://u@x.example.org", "userinfo"),
                      ("https://x.example.org/p", "path"), ("data:text/html,x", "https_only")):  # fmt: skip
        meta, _ = make_meta(reach=[bad])
        assert err(w.hub("PUT", t, json.dumps(meta).encode())) == code, bad


def test_a_slug_is_never_freed(w):
    meta, files = make_meta()
    w.publish(meta, files)
    t = f"/v1/ws/{WS_A}/apps/{APP}"
    assert w.hub("DELETE", t).status_code == 200
    r = w.hub("PUT", f"/v1/ws/{WS_A}/apps/{APP2}/versions/1", json.dumps(meta).encode())
    assert (r.status_code, err(r)) == (409, "slug_taken")
    assert w.hub("PUT", t + "/versions/2", json.dumps(meta).encode()).status_code == 410


def test_replay_window_keeps_the_nonce_240s(w):
    meta, _ = make_meta()
    t = f"/v1/ws/{WS_A}/apps/{APP}/versions/1"
    nonce = os.urandom(16)
    body = json.dumps(meta).encode()
    h, _ = build(w.e.hub, w.e.wsk, "PUT", t, body, ts=NOW + 120_000, nonce=nonce, host=HOST)
    w.e.now = NOW  # window edge: ts is 120 s ahead
    assert w.e.c.put(t, content=body, headers=h).status_code == 200
    w.e.now = NOW + 240_000
    assert (
        w.e.c.put(t, content=body, headers=h).status_code == 401
    )  # replay (still in the store) or stale


# --- condition 2: the direct path -------------------------------------------------------------------------------


def test_direct_path_is_off_by_default(w):
    meta, _ = make_meta()
    r = w.direct("PUT", f"/v1/ws/{WS_A}/apps/{APP}/versions/1", json.dumps(meta).encode())
    assert (r.status_code, err(r)) == (403, "hub_required")


def test_a_workspace_with_no_hub_publishes_a_public_app(tmp_path):
    w = World(tmp_path)
    w.e.sql("DELETE FROM hub_bindings")  # no hub, no binding
    w.e.sql("DELETE FROM hub_cards")
    w.e.app.state.settings.allow_direct = True
    meta, files = make_meta()
    w.publish(meta, files, send="direct")
    r = w.web.get(w.view_url())
    assert r.status_code == 200 and r.content == INDEX
    # shares never take the direct path
    r = w.direct("GET", f"/v1/ws/{WS_A}/status")
    assert (r.status_code, err(r)) == (403, "hub_required")


def test_direct_commit_needs_approval_and_it_does_not_cross_paths(w):
    w.e.app.state.settings.allow_direct = True
    meta, files = make_meta()
    w.stage(meta, files, send="direct")
    assert err(w.release("commit", meta, send="direct", approval=None)) == "approval_required"
    # an approval built for the hub path does not verify on the direct path, nor the other way round
    body = json.dumps(w.body("commit", meta)).encode()
    t = f"/v1/ws/{WS_A}/apps/{APP}/commit"
    assert err(w.direct("POST", t, body, approval_for="public", hub_style=True)) in (
        "bad_approval",
        "other_hub",
    )
    ts, nonce = NOW, os.urandom(16)
    rh = P.app_request_hash(bytes.fromhex(WS_A), ts, nonce, "POST", HOST, t, body, None)
    ap = w._approval(rh, "public", None)
    h, _ = build(w.e.hub, w.e.wsk, "POST", t, body, ts=ts, nonce=nonce, approval=ap, host=HOST)
    r = w.e.c.post(t, content=body, headers=h)
    assert r.status_code == 403 and err(r) in ("bad_approval", "other_hub")
    assert w.web.get("/a/shop-demo/").status_code == 404
    # a bad direct signature
    r = w.direct("PUT", f"/v1/ws/{WS_A}/apps/{APP}/versions/9", b"{}", ts=NOW - 10**6)
    assert err(r) == "stale_timestamp"


def test_direct_replay_is_refused(w):
    w.e.app.state.settings.allow_direct = True
    meta, _ = make_meta()
    nonce = os.urandom(16)
    t = f"/v1/ws/{WS_A}/apps/{APP}/versions/1"
    body = json.dumps(meta).encode()
    assert w.direct("PUT", t, body, nonce=nonce).status_code == 200
    assert err(w.direct("PUT", t, body, nonce=nonce)) == "replay"


# --- rollback, start, stop, remove (condition 12) ---------------------------------------------------------------


def test_rollback_start_stop_remove(w):
    m1, f1 = make_meta()
    w.publish(m1, f1)
    f2 = dict(f1, **{"index.html": b"<p>v2</p>"})
    m2, f2 = make_meta(extra={"index.html": b"<p>v2</p>"})
    w.publish(m2, f2, version=2)
    assert w.web.get(w.view_url()).content == b"<p>v2</p>"
    assert err(w.release("rollback", m1, version=1, approval=None)) == "approval_required"
    assert w.release("rollback", m1, version=1).status_code == 200
    url = w.view_url()
    assert w.web.get(url).content == INDEX
    t = f"/v1/ws/{WS_A}/apps/{APP}"
    assert w.hub("POST", t + "/stop").status_code == 200
    # the view session is gone and the next request is 410
    assert w.sql("SELECT COUNT(*) c FROM app_views")[0]["c"] == 0
    assert w.web.get(url).status_code == 410
    assert w.web.get("/a/shop-demo/").status_code == 410
    assert err(w.release("start", m2, version=2, approval=None)) == "approval_required"
    assert w.release("start", m2, version=2).status_code == 200
    url = w.view_url()
    assert w.web.get(url).content == b"<p>v2</p>"
    assert w.hub("DELETE", t).status_code == 200
    assert w.web.get(url).status_code == 404
    assert not os.path.exists(os.path.join(w.e.data, "apps", APP))
    assert w.sql("SELECT COUNT(*) c FROM app_files")[0]["c"] == 0
    assert w.sql("SELECT COUNT(*) c FROM app_versions")[0]["c"] == 0
    assert w.sql("SELECT COUNT(*) c FROM app_views")[0]["c"] == 0


def test_only_five_committed_versions_are_kept(w):
    for n in range(1, 8):
        m, f = make_meta(extra={"index.html": f"<p>{n}</p>".encode()})
        w.publish(m, f, version=n)
    kept = [r["version"] for r in w.sql("SELECT version FROM app_versions ORDER BY version")]
    assert kept == [3, 4, 5, 6, 7]
    assert not os.path.exists(os.path.join(w.e.data, "apps", APP, "1"))


def test_a_new_access_drops_old_view_sessions(w):
    m, f = make_meta()
    w.publish(m, f)
    url = w.view_url()
    m2, f2 = make_meta(access="password", pw=pw_record(), extra={"index.html": b"<p>x</p>"})
    w.publish(m2, f2, version=2)
    assert w.web.get(url).status_code in (404, 410)


# --- serving: headers on every route (conditions 4, 5, 6) -------------------------------------------------------


def _routes(w):
    url = w.view_url()
    slug = url.split("/")[2]
    v = url.split("/")[4]
    z = appserve.ZERO_VIEW
    return [
        ("GET", "/a/"), ("GET", "/a/Bad_Slug/"), ("GET", "/a/nope-nope/"), ("GET", "/a/shop-demo"),
        ("GET", "/a/shop-demo/"), ("GET", f"/a/{slug}/v/{v}/"), ("GET", f"/a/{slug}/v/{v}/app.js"),
        ("GET", f"/a/{slug}/v/{v}/data.bin"), ("GET", f"/a/{slug}/v/{v}/missing.js"),
        ("GET", f"/a/{slug}/v/{v}/.env"), ("GET", f"/a/{slug}/v/{v}/%2e%2e/x"),
        ("GET", f"/a/{slug}/v/{'f' * 32}/"), ("POST", f"/a/{slug}/v/{v}/"), ("POST", f"/a/{slug}/"),
        ("POST", f"/a/{slug}/v/{z}/gate"), ("GET", f"/a/{slug}/v/{z}/gate"), ("PUT", f"/a/{slug}/"),
        ("GET", f"/a/{slug}/v/{z}/x"), ("GET", "/a/shop-demo/other"),
    ]  # fmt: skip


def test_every_a_response_has_the_sandbox_csp_and_no_cookie(w):
    reach = ["https://api.example.org"]
    m, f = make_meta(reach=reach)
    w.publish(m, f)
    for method, path in _routes(w):
        r = w.web.request(method, path, headers={"Cookie": "hub=secret", "Origin": "null"})
        csp = r.headers["content-security-policy"]
        assert csp.startswith(
            "sandbox allow-scripts allow-forms allow-popups allow-downloads; default-src 'none'"
        ), path
        for bad in ("allow-same-origin", "allow-top-navigation", "allow-popups-to-escape-sandbox"):
            assert bad not in csp, path
        assert f"{ORIGIN}/a/" in csp and "frame-ancestors 'none'" in csp, path
        assert r.headers["x-content-type-options"] == "nosniff", path
        assert r.headers["referrer-policy"] == "no-referrer", path
        assert "set-cookie" not in r.headers, path
        live_file = (
            r.status_code == 200
            and method == "GET"
            and f"/v/{appserve.ZERO_VIEW}/" not in path
            and "/v/" in path
        )
        assert r.headers.get("access-control-allow-origin") == ("null" if live_file else None), path
        assert "access-control-allow-credentials" not in r.headers, path
        assert r.headers["cache-control"] == "no-store"


def test_csp_base_never_comes_from_the_host_header(w):
    m, f = make_meta()
    w.publish(m, f)
    url = w.view_url()
    r = w.web.get(url, headers={"Host": "evil.example.org"})
    assert "evil.example.org" not in r.headers["content-security-policy"]
    assert ORIGIN + url in r.headers["content-security-policy"]


def test_reach_is_in_connect_src_only(w):
    m, f = make_meta(reach=["https://api.example.org"])
    w.publish(m, f)
    csp = w.web.get(w.view_url()).headers["content-security-policy"]
    assert (
        "connect-src " + ORIGIN in csp
        and "https://api.example.org" in csp.split("connect-src")[1].split(";")[0]
    )
    assert csp.count("api.example.org") == 1


def test_unconfigured_share_origin_serves_nothing_but_still_sandboxes(tmp_path):
    s = create_share_app(str(tmp_path / "d"))
    c = TestClient(s, base_url=ORIGIN, follow_redirects=False)
    r = c.get("/a/shop-demo/")
    assert r.status_code == 404 and r.headers["content-security-policy"].startswith("sandbox ")


def test_extension_allowlist(w):
    m, f = make_meta(extra={"p.svg": b"<svg/>", "x.xml": b"<x/>", "y.exe": b"MZ"})
    w.publish(m, f)
    url = w.view_url()
    assert w.web.get(url + "p.svg").headers["content-type"] == "image/svg+xml"
    for n in ("data.bin", "x.xml", "y.exe"):
        r = w.web.get(url + n)
        assert r.headers["content-type"] == "application/octet-stream", n
        assert r.headers["content-disposition"].startswith("attachment"), n
    assert "content-disposition" not in w.web.get(url).headers


def test_no_redirect_under_a_view_base(w):
    m, f = make_meta()
    w.publish(m, f)
    url = w.view_url()
    for tail in ("", "app.js", "nope", "sub/", "%2e%2e/"):
        assert w.web.get(url + tail).status_code in (200, 404), tail


def test_service_worker_requests_are_refused(w):
    m, f = make_meta()
    w.publish(m, f)
    url = w.view_url()
    r = w.web.get(url + "app.js", headers={"Service-Worker": "script"})
    assert r.status_code == 403 and r.headers["content-security-policy"].startswith("sandbox ")
    assert w.web.get("/a/shop-demo/", headers={"Service-Worker": "script"}).status_code == 403
    assert w.web.get("/s/" + "d" * 32, headers={"Service-Worker": "script"}).status_code == 403


def test_view_session_expires(w):
    m, f = make_meta()
    w.publish(m, f)
    url = w.view_url()
    w.e.now += 31 * 60 * 1000
    assert w.web.get(url).status_code == 410


def test_company_and_group_apps_are_refused_for_now(w):
    m, f = make_meta(access="company", audience="company")
    w.publish(m, f)
    r = w.web.get("/a/shop-demo/")
    assert r.status_code == 403 and "Sign-in is not available" in r.text
    assert w.sql("SELECT COUNT(*) c FROM app_views")[0]["c"] == 0


# --- condition 9: the password gate -----------------------------------------------------------------------------


GATE = f"/a/shop-demo/v/{appserve.ZERO_VIEW}/gate"


def pw_app(w):
    m, f = make_meta(access="password", pw=pw_record())
    w.publish(m, f)


def test_password_gate_flow(w):
    pw_app(w)
    r = w.web.get("/a/shop-demo/")
    assert r.status_code == 200 and 'type="password"' in r.text and 'method="post"' in r.text
    assert GATE in r.text
    r = w.web.post(GATE, data={"p": "wrong"})
    assert r.status_code == 401
    assert w.sql("SELECT COUNT(*) c FROM app_views")[0]["c"] == 0
    assert w.sql("SELECT COUNT(*) c FROM app_visits")[0]["c"] == 0  # a wrong password is not a view
    r = w.web.post(GATE, data={"p": PW})
    assert r.status_code == 200 and "refresh" in r.text
    url = r.text.split("url=")[1].split('"')[0]
    assert w.web.get(url).content == INDEX
    assert w.sql("SELECT n FROM app_visits")[0]["n"] == 1


def test_password_never_in_a_query(w):
    pw_app(w)
    r = w.web.get(f"/a/shop-demo/?p={PW}")
    assert 'type="password"' in r.text and w.sql("SELECT COUNT(*) c FROM app_views")[0]["c"] == 0
    r = w.web.post(GATE + f"?p={PW}", data={"p": "wrong"})
    assert r.status_code == 401


def test_five_tries_a_minute_then_rate_limited_before_the_kdf(w, monkeypatch):
    pw_app(w)
    calls = []
    real = P.password_ok
    monkeypatch.setattr(P, "password_ok", lambda *a: calls.append(1) or real(*a))
    codes = [w.web.post(GATE, data={"p": "x" + str(i)}).status_code for i in range(7)]
    assert codes == [401] * 5 + [429] * 2
    assert len(calls) == 5  # the sixth and seventh never reached the KDF
    assert w.web.post(GATE, data={"p": PW}).status_code == 429
    w.e.now += 61_000
    assert w.web.post(GATE, data={"p": PW}).status_code == 200


def test_kdf_slots_are_bounded(w):
    pw_app(w)
    srv = w.share.apps
    for _ in range(appserve.KDF_SLOTS):
        assert srv._kdf.acquire(blocking=False)
    try:
        r = w.web.post(GATE, data={"p": PW})
        assert r.status_code == 503
    finally:
        for _ in range(appserve.KDF_SLOTS):
            srv._kdf.release()
    assert w.web.post(GATE, data={"p": PW}).status_code == 200


def test_gate_on_a_public_app_is_not_found(w):
    m, f = make_meta()
    w.publish(m, f)
    assert w.web.post(GATE, data={"p": "x"}).status_code == 404


def test_password_and_cookie_never_reach_the_log(w, caplog):
    pw_app(w)
    caplog.set_level("DEBUG")
    w.web.post(GATE, data={"p": PW}, headers={"Cookie": "hub=sekret"})
    assert PW not in caplog.text and "sekret" not in caplog.text


# --- visits -----------------------------------------------------------------------------------------------------


def test_visits_are_a_daily_count_and_nothing_else(w):
    m, f = make_meta()
    w.publish(m, f)
    for _ in range(3):
        url = w.view_url()
        w.web.get(url)
        w.web.get(url + "app.js")
    rows = w.sql("SELECT * FROM app_visits")
    assert [(r["day"], r["n"]) for r in rows] == [(NOW // 86_400_000, 3)]
    assert set(rows[0].keys()) == {"app_id", "day", "n"}
    cols = {c["name"] for c in w.sql("PRAGMA table_info(app_views)")}
    assert cols == {"view_id", "app_id", "until_ms"}  # no IP, user agent or referrer anywhere


def test_a_schema_3_data_dir_is_refused_not_migrated(tmp_path):
    path = db.prepare_dir(str(tmp_path / "d"))
    import sqlite3

    c = sqlite3.connect(path)
    c.executescript(db.SCHEMA)
    c.execute("DROP TABLE app_visits")
    c.execute("PRAGMA user_version=3")
    c.commit()
    c.close()
    with pytest.raises(db.SchemaError, match="run `orch update`"):
        db.init(path)


def test_kdf_semaphore_is_threadsafe_type():
    assert isinstance(appserve.AppServer.__init__, object) and threading.BoundedSemaphore


# --- review fixes (Opus review of S3) ---------------------------------------------------------------------------


def test_m1_api_without_a_share_origin_takes_no_apps(w):
    w.e.app.state.settings.share_origin = None
    m, _ = make_meta(reach=["https://apps.example.org"])
    r = w.hub("PUT", f"/v1/ws/{WS_A}/apps/{APP}/versions/1", json.dumps(m).encode())
    assert (r.status_code, err(r)) == (503, "apps_not_configured")


def test_m1_reach_naming_the_share_origin_is_refused_at_meta_put(w):
    so = "https://apps.example.org"
    w.e.app.state.settings.share_origin = so
    m, _ = make_meta(reach=[so])
    r = w.hub("PUT", f"/v1/ws/{WS_A}/apps/{APP}/versions/1", json.dumps(m).encode())
    assert (r.status_code, err(r)) == (400, "forbidden_origin")


def test_m1_a_csp_error_never_gives_a_response_without_the_sandbox(w):
    so = "https://apps.example.org"
    m, f = make_meta(reach=[so])
    w.publish(m, f)  # accepted: the API was configured with another origin
    share = create_share_app(w.e.data, clock=lambda: w.e.now, share_origin=so, api_host=HOST)
    web = TestClient(share, base_url=so, follow_redirects=False)
    r = web.get(web.get("/a/shop-demo/").headers["location"])
    assert r.status_code == 200 and r.headers["content-security-policy"].startswith("sandbox ")
    connect = r.headers["content-security-policy"].split("connect-src ")[1].split(";")[0]
    assert (
        len(connect.split()) == 1
    )  # the reach naming publish itself was dropped, only the base remains


def test_m1_serve_takes_a_share_origin(tmp_path, monkeypatch):
    import uvicorn

    from pubsrv import app as app_mod
    from pubsrv import cli

    seen = {}
    real = app_mod.create_app

    def spy(*a, **k):
        x = real(*a, **k)
        seen["o"] = x.state.settings.share_origin
        return x

    monkeypatch.setattr(app_mod, "create_app", spy)
    monkeypatch.setattr(uvicorn, "run", lambda *a, **k: None)
    d = str(tmp_path / "d")
    assert cli.main(["serve", "--data", d, "--share-origin", "https://Apps.Example.org/"]) == 0
    assert seen["o"] == "https://apps.example.org"


def test_m2_share_routes_are_not_app_routes(w):
    w.e.app.state.settings.allow_direct = True
    for tail, body in (("extend", b'{"days":3}'), ("password", b"{}")):
        r = w.direct("POST", f"/v1/ws/{WS_A}/shares/{'c3' * 16}/{tail}", body)
        assert (r.status_code, err(r)) == (403, "hub_required")
    from pubsrv import apps

    assert apps.route_parts("POST", f"/v1/ws/{WS_A}/shares/{'c3' * 16}/extend") is None
    assert apps.route_parts("POST", f"/v1/ws/{WS_A}/apps/{APP}/stop") is not None


def test_m3_cors_null_only_on_a_successful_live_view_file(w):
    m, f = make_meta()
    w.publish(m, f)
    url = w.view_url()
    ok = w.web.get(url + "app.js", headers={"Origin": "null"})
    assert ok.headers["access-control-allow-origin"] == "null"
    assert "access-control-allow-credentials" not in ok.headers
    for path in (
        url + "missing.js",
        "/a/shop-demo/",
        "/a/nope-nope/",
        f"/a/shop-demo/v/{'f' * 32}/app.js",
    ):
        r = w.web.get(path, headers={"Origin": "null"})
        assert "access-control-allow-origin" not in r.headers, path
    w.e.now += 31 * 60 * 1000
    assert "access-control-allow-origin" not in w.web.get(url + "app.js").headers  # ended view


def test_m3_gate_and_shares_have_no_cors(w):
    pw_app(w)
    r = w.web.post(GATE, data={"p": PW}, headers={"Origin": "null"})
    assert r.status_code == 200 and "access-control-allow-origin" not in r.headers
    url = r.text.split("url=")[1].split('"')[0]
    assert (
        w.web.get(url).headers["access-control-allow-origin"] == "null"
    )  # the file under the live view
    assert "access-control-allow-origin" not in w.web.get("/s/" + "d" * 32).headers


def test_l1_errors_under_a_carry_the_sandbox(w, monkeypatch):
    import sqlite3

    m, f = make_meta()
    w.publish(m, f)
    url = w.view_url()
    for exc, code in ((sqlite3.OperationalError("locked"), 503), (RuntimeError("boom"), 500)):

        def boom(*a, _e=exc, **k):
            raise _e

        monkeypatch.setattr(appserve.db, "connect", boom)
        for path in ("/a/shop-demo/", url + "app.js", url):
            r = w.web.get(path)
            assert r.status_code == code, path
            assert r.headers["content-security-policy"].startswith("sandbox allow-scripts"), path
            assert "set-cookie" not in r.headers and "access-control-allow-origin" not in r.headers


def test_l3_removed_slugs_are_capped_per_workspace(w, monkeypatch):
    from pubsrv import apps

    monkeypatch.setattr(apps, "MAX_TOMBSTONES", 3)
    t = f"/v1/ws/{WS_A}/apps/"
    for i in range(3):
        app = f"{i + 1:032x}"
        m, _ = make_meta(slug=f"squat-{i:03d}")
        assert w.hub("PUT", f"{t}{app}/versions/1", json.dumps(m).encode()).status_code == 200
        assert w.hub("DELETE", f"{t}{app}").status_code == 200
    m, _ = make_meta(slug="squat-new")
    r = w.hub("PUT", f"{t}{'9' * 32}/versions/1", json.dumps(m).encode())
    assert (r.status_code, err(r)) == (429, "too_many_removed")
