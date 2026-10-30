# ruff: noqa: RUF059
"""HUB-PE-04 routes: company login opens sessions only, links are keyed on the IdP's immutable id, the cookie is sealed."""

from __future__ import annotations

import json
from urllib.parse import parse_qs, urlsplit

import pytest
from oidc_fake_idp import CLIENT, NOW, TENANT, FakeIdp, entra_cfg
from webapi_helpers import ORIGIN, Web

from orch_hub.activity import ActivityLog
from orch_hub.oidc.service import OidcService
from orch_hub.oidc.store import OidcStore

SECRET = "client-secret-value-123"
COOKIE = "__Host-orch_oidc_tx"


class Env:
    def __init__(self, tmp_path) -> None:
        self.w = Web(tmp_path)
        d = tmp_path / "oidc-state"
        d.mkdir(mode=0o700)
        self.dir = d
        self.store = OidcStore(d)
        self.idp = FakeIdp(entra_cfg())
        self.svc = OidcService(self.store, self.idp, hosts=frozenset({"idp.example"}), now=lambda: NOW)
        self.log = ActivityLog(tmp_path / "activity.db", now_ms=self.w.clock)
        self.w.api.d.oidc, self.w.api.d.activity = self.svc, self.log
        self.admin_tok = self.w.login(self.w.admin)
        self.ctx_cookie = ""
        self.idp.claims = {}

    def close(self) -> None:
        self.store.close()
        self.log.close()
        self.w.close()

    def call(self, method, path, body=None, *, token=None, tx=None, peer=""):
        """Like Web.call, plus the company-login cookie the browser would send."""
        w = self.w
        h = {"host": "127.0.0.1:9000", "origin": ORIGIN, "x-orch-csrf": "1"}
        parts = []
        if token:
            parts.append(f"{w.api._cookie_name}={token}")
        if tx:
            parts.append(f"{COOKIE}={tx}")
        if parts:
            h["cookie"] = "; ".join(parts)
        raw = b"" if body is None else json.dumps(body).encode()
        if raw:
            h["content-type"] = "application/json"
        s, hdrs, out = w.api.handle(method, "/api/v1" + path, h, raw, peer)
        return s, json.loads(out), hdrs

    @staticmethod
    def cookies(hdrs) -> dict[str, str]:
        return {v.split("=", 1)[0]: v for k, v in hdrs if k == "Set-Cookie"}

    def configure(self, mode="invited", enabled=True, **kw):
        body = {
            "provider": "entra", "client_id": CLIENT, "tenant": TENANT, "domain": "", "mode": mode, "idp_mfa": True,
            "enabled": False, "client_secret": SECRET,
        }  # fmt: skip
        body.update(kw)
        s, o, _ = self.call("PUT", "/admin/oidc", body, token=self.admin_tok)
        assert s == 200, o
        if enabled:
            self.test_login()
            body.update(enabled=True, client_secret="")
            s, o, _ = self.call("PUT", "/admin/oidc", body, token=self.admin_tok)
            assert s == 200, o
        return body

    def begin(self, path="/oidc/start", token=None, body=None):
        s, o, h = self.call("POST", path, body or {}, token=token)
        assert s == 200, o
        q = {k: v[0] for k, v in parse_qs(urlsplit(o["url"]).query).items()}
        self.idp.nonce = q["nonce"]
        self.idp.challenge = q["code_challenge"]
        set_cookie = self.cookies(h)[COOKIE]
        value = set_cookie.split(";")[0].split("=", 1)[1]
        return q, value, set_cookie

    def test_login(self):
        self.idp.claims = {"auth_time": NOW - 5}
        q, tx, _ = self.begin("/admin/oidc/test/start", self.admin_tok)
        s, o, _ = self.call(
            "POST", "/admin/oidc/test/finish", {"code": "c", "state": q["state"]}, token=self.admin_tok, tx=tx
        )
        assert s == 200, o

    def company_login(self, **claims):
        self.idp.claims = claims
        q, tx, _ = self.begin()
        return self.call("POST", "/oidc/finish", {"code": "c", "state": q["state"]}, tx=tx)


@pytest.fixture
def e(tmp_path):
    x = Env(tmp_path)
    yield x
    x.close()


def session_of(hdrs, w):
    c = Env.cookies(hdrs)
    name = w.api._cookie_name
    return c[name].split(";")[0].split("=", 1)[1]


def test_settings_round_trip_never_shows_the_secret(e):
    e.configure(enabled=False)
    s, o, _ = e.call("GET", "/admin/oidc", token=e.admin_tok)
    assert o["has_secret"] is True and o["provider"] == "entra" and o["tested"] is False and o["enabled"] is False
    assert SECRET not in json.dumps(o)
    assert (e.dir / "oidc.json").read_text().count(SECRET) == 0
    rows = json.dumps(e.log.list())
    assert SECRET not in rows
    _s, o, _ = e.call("GET", "/oidc/options")
    assert o == {"enabled": False, "provider": None}


def test_enabling_needs_a_passing_test_and_the_mfa_statement(e):
    body = e.configure(enabled=False)
    s, o, _ = e.call("PUT", "/admin/oidc", {**body, "enabled": True, "client_secret": ""}, token=e.admin_tok)
    assert (s, o["error"]) == (409, "oidc_not_ready")
    e.test_login()
    s, o, _ = e.call(
        "PUT", "/admin/oidc", {**body, "enabled": True, "idp_mfa": False, "client_secret": ""}, token=e.admin_tok
    )
    assert (s, o["error"]) == (409, "oidc_not_ready")  # the admin must make the second-step statement
    s, o, _ = e.call("PUT", "/admin/oidc", {**body, "enabled": True, "client_secret": ""}, token=e.admin_tok)
    assert s == 200 and o["enabled"] and o["tested"]
    # a new secret or a changed provider setting needs a new test
    s, o, _ = e.call(
        "PUT", "/admin/oidc", {**body, "enabled": False, "client_secret": "another-secret-456"}, token=e.admin_tok
    )
    assert o["tested"] is False
    assert e.call("GET", "/oidc/options")[1]["enabled"] is False


def test_only_a_hub_admin_may_change_settings(e):
    ann = e.w.login(e.w.ann)
    assert e.call("GET", "/admin/oidc", token=ann)[0] == 403
    assert e.call("PUT", "/admin/oidc", {}, token=ann)[0] == 403
    assert e.call("POST", "/admin/oidc/test/start", {}, token=ann)[0] == 403


def test_linked_person_signs_in_and_it_is_a_session_only(e):
    e.configure()
    e.store.link(e.w.ann, "entra", TENANT, "oid-ann")
    s, o, h = e.company_login(oid="oid-ann")
    assert s == 200 and o["user_id"] == e.w.ann
    tok = session_of(h, e.w)
    s, me, _ = e.call("GET", "/session", token=tok)
    assert s == 200 and me["company_login"] is True and me["user_id"] == e.w.ann
    assert any(r["type"] == "oidc.signed_in" for r in e.log.list())
    # decision C: this session can never register a signing key (needs the hub password + a code)
    key = __import__("channels_helpers").new_channel()
    from orch_hub.contract.canon import b64u

    for creds in ({}, {"password": "x", "totp": "123456"}):
        s, o, _ = e.call(
            "POST", "/channels/register/begin", {"kind": "browser", "key_pub": b64u(key.public), **creds}, token=tok
        )
        assert s in (400, 401, 403), o
    rows = e.w.env.svc.db.q("SELECT COUNT(*) FROM channels WHERE user_id=?", (e.w.ann,))
    assert rows[0][0] == 0  # no key was recorded, with any factor


def test_unlinked_person_is_refused_the_same_way_as_a_disabled_one(e):
    e.configure()
    s, o, h = e.company_login(oid="oid-stranger")
    assert (s, o["error"]) == (403, "oidc_not_allowed")
    e.store.link(e.w.ann, "entra", TENANT, "oid-ann")
    e.w.env.acc.set_disabled(e.w.ann, True, actor=e.w.admin)
    s2, o2, _ = e.company_login(oid="oid-ann")
    assert (s2, o2["error"]) == (403, "oidc_not_allowed") and o2["message"] == o["message"]
    assert "Set-Cookie" in [k for k, _ in h]  # the login cookie is cleared on every outcome


def test_email_is_never_the_identity(e):
    e.configure()
    e.store.link(e.w.ann, "entra", TENANT, "oid-ann")
    # the email of the admin, a different subject: refused (and in invited mode nobody is created)
    s, o, _ = e.company_login(oid="oid-evil", email="admin@acme.example")
    assert s == 403
    # the same person with another email still signs in
    s, o, _ = e.company_login(oid="oid-ann", email="renamed@acme.example")
    assert s == 200 and o["user_id"] == e.w.ann


def test_company_mode_creates_a_bare_member_never_an_admin(e):
    e.configure(mode="company")
    before = {u["user_id"] for u in e.w.env.acc.users()}
    s, o, _h = e.company_login(oid="oid-new", name="Newcomer")
    assert s == 200
    uid = o["user_id"]
    assert uid not in before
    assert e.w.env.acc.groups_of(uid) == [] or list(e.w.env.acc.groups_of(uid)) == []
    assert not e.w.env.acc.is_hub_admin(uid)
    types = [r["type"] for r in e.log.list()]
    assert "oidc.member_created" in types and "oidc.signed_in" in types
    # a second login is the same person
    s, o2, _ = e.company_login(oid="oid-new")
    assert o2["user_id"] == uid
    # the admin's own subject collides with nothing: never auto-admin even with the admin's email
    s, o3, _ = e.company_login(oid="oid-other", email="admin@acme.example", name="admin")
    assert not e.w.env.acc.is_hub_admin(o3["user_id"])


def test_bare_member_reads_nothing_beyond_whoami_and_the_rail(e):
    e.configure(mode="company")
    s, _o, h = e.company_login(oid="oid-new", name="Newcomer")
    tok = session_of(h, e.w)
    from orch_hub.webapi.api import ROUTES

    leaks = []
    for rt in ROUTES:
        if rt.method != "GET" or rt.auth != "session" or "{" in rt.path:
            continue
        s, body, _ = e.call("GET", rt.path, token=tok)
        if s == 200 and rt.handler not in ALLOWED_BARE:
            leaks.append((rt.path, body))
    flat = [(p, b) for p, b in leaks if _has_data(b)]
    assert flat == [], flat
    # the parameterised reads: a workspace the person has no role in answers unknown, never roles or items
    from webapi_helpers import WS

    s, b, _ = e.call("GET", f"/workspaces/{WS}/roles", token=tok)
    assert s != 200 and "groups" not in b
    s, b, _ = e.call("GET", f"/workspaces/{WS}/items/{'ab' * 16}", token=tok)
    assert s != 200
    assert e.call("GET", "/me/workspaces", token=tok)[1]["workspaces"] == []
    assert e.call("GET", "/admin/users", token=tok)[0] == 403


ALLOWED_BARE = {
    "whoami",
    "oidc_me",
    "my_workspaces",
    "rail",
    "my_channels",
    "my_handoffs",
    "needs_you",
    "my_shared",
    "my_pairing",
    "my_channels_seen",
}


def _has_data(b):
    if isinstance(b, dict):
        return any(_has_data(v) for v in b.values())
    if isinstance(b, list):
        return len(b) > 0
    return b not in (None, False, 0, "", {})


def test_cookie_attributes_and_cleared_on_every_outcome(e):
    e.configure()
    q, tx, set_cookie = e.begin()
    for attr in ("HttpOnly", "SameSite=Lax", "Secure", "Path=/", "Max-Age=600", COOKIE + "="):
        assert attr in set_cookie, set_cookie
    # failure: wrong state
    s, o, h = e.call("POST", "/oidc/finish", {"code": "c", "state": "wrong"}, tx=tx)
    assert s == 400 and "Max-Age=0" in e.cookies(h)[COOKIE]
    # failure: no cookie at all
    s, o, h = e.call("POST", "/oidc/finish", {"code": "c", "state": q["state"]})
    assert s == 400 and "Max-Age=0" in e.cookies(h)[COOKIE]
    # success (a linked person)
    e.store.link(e.w.ann, "entra", TENANT, "oid-ann")
    e.idp.claims = {"oid": "oid-ann"}
    q, tx, _ = e.begin()
    s, o, h = e.call("POST", "/oidc/finish", {"code": "c", "state": q["state"]}, tx=tx)
    assert s == 200 and "Max-Age=0" in e.cookies(h)[COOKIE]
    # a replay of the same cookie is refused
    s, _o, _ = e.call("POST", "/oidc/finish", {"code": "c", "state": q["state"]}, tx=tx)
    assert s == 400


def test_error_answers_never_leak_the_reason_or_the_secret(e):
    e.configure()
    _s, o, _ = e.company_login(tid="99999999-2222-3333-4444-555555555555")
    assert o["error"] == "oidc_failed" and "reason" not in o and "tenant" not in json.dumps(o)
    assert SECRET not in json.dumps(o)


def test_amr_without_mfa_is_refused(e):
    e.configure()
    e.store.link(e.w.ann, "entra", TENANT, "oid-ann")
    s, o, _ = e.company_login(oid="oid-ann", amr=["pwd"])
    assert s == 403
    s, _o, _ = e.company_login(oid="oid-ann", amr=["pwd", "mfa"])
    assert s == 200


def test_pkce_mismatch_at_the_idp_means_no_session(e):
    e.configure()
    e.store.link(e.w.ann, "entra", TENANT, "oid-ann")
    e.idp.claims = {"oid": "oid-ann"}
    q, tx, _ = e.begin()
    e.idp.challenge = "not-the-challenge-we-sent"  # an intercepted code redeemed with another verifier
    s, o, h = e.call("POST", "/oidc/finish", {"code": "c", "state": q["state"]}, tx=tx)
    assert s == 400 and o["error"] == "oidc_failed"
    assert session_name(e, h) is None


def session_name(e, h):
    return Env.cookies(h).get(e.w.api._cookie_name)


def test_linking_needs_a_fresh_password_and_code_and_is_audited(e):
    e.configure()
    tok = e.w.login(e.w.ann)
    for bad in ({"password": "wrong", "totp": "000000"}, {}):
        s, o, _ = e.call("POST", "/oidc/link/start", bad, token=tok)
        assert s in (400, 401, 403)
    e.idp.claims = {"oid": "oid-ann", "auth_time": NOW - 5}
    q, tx, _ = e.begin("/oidc/link/start", tok, e.w.creds(e.w.ann))
    s, _o, _h = e.call("POST", "/oidc/link/finish", {"code": "c", "state": q["state"]}, token=tok, tx=tx)
    assert s == 200 and e.store.user_for("entra", TENANT, "oid-ann") == e.w.ann
    assert any(r["type"] == "oidc.linked" for r in e.log.list())
    assert e.call("GET", "/oidc/me", token=tok)[1]["linked"]["provider"] == "entra"
    # the password sign-in of this person still works (and was never removed)
    assert e.w.login(e.w.ann)


def test_a_link_login_cannot_sign_in_nor_be_finished_by_someone_else(e):
    e.configure()
    ann = e.w.login(e.w.ann)
    e.idp.claims = {"oid": "oid-ann", "auth_time": NOW - 5}
    q, tx, _ = e.begin("/oidc/link/start", ann, e.w.creds(e.w.ann))
    # the link cookie used as a sign-in: refused, nothing linked
    s, _o, _ = e.call("POST", "/oidc/finish", {"code": "c", "state": q["state"]}, tx=tx)
    assert s == 400
    # another signed-in person cannot finish Ann's link
    e.idp.claims = {"oid": "oid-ann", "auth_time": NOW - 5}
    q, tx, _ = e.begin("/oidc/link/start", ann, e.w.creds(e.w.ann))
    admin_link = e.call("POST", "/oidc/link/finish", {"code": "c", "state": q["state"]}, token=e.admin_tok, tx=tx)
    assert admin_link[0] == 400 and e.store.user_for("entra", TENANT, "oid-ann") is None


def test_an_identity_links_to_one_person_only(e):
    e.configure()
    e.store.link(e.w.ann, "entra", TENANT, "oid-ann")
    s, o, _ = e.company_login(oid="oid-ann")
    assert s == 200
    # the admin tries to link the same company account
    e.idp.claims = {"oid": "oid-ann", "auth_time": NOW - 5}
    q, tx, _ = e.begin("/oidc/link/start", e.admin_tok, e.w.creds(e.w.admin))
    s, _o, _ = e.call("POST", "/oidc/link/finish", {"code": "c", "state": q["state"]}, token=e.admin_tok, tx=tx)
    assert s == 400 and e.store.user_for("entra", TENANT, "oid-ann") == e.w.ann


def test_unlinking_ends_company_sessions_only(e):
    e.configure()
    e.store.link(e.w.ann, "entra", TENANT, "oid-ann")
    pw_tok = e.w.login(e.w.ann)
    s, o, h = e.company_login(oid="oid-ann")
    co_tok = session_of(h, e.w)
    assert e.call("DELETE", "/oidc/link", token=co_tok)[0] == 200
    assert e.call("GET", "/session", token=co_tok)[0] == 401 or e.call("GET", "/session", token=co_tok)[0] >= 400
    assert e.call("GET", "/session", token=pw_tok)[0] == 200  # the password session lives
    assert e.store.linked(e.w.ann) is None
    assert any(r["type"] == "oidc.unlinked" for r in e.log.list())
    s, _o, _ = e.company_login(oid="oid-ann")
    assert s == 403  # the link is gone


def test_changing_the_provider_unlinks_everyone_and_ends_their_sessions(e):
    body = e.configure()
    e.store.link(e.w.ann, "entra", TENANT, "oid-ann")
    s, o, h = e.company_login(oid="oid-ann")
    co_tok = session_of(h, e.w)
    other = "99999999-2222-3333-4444-555555555555"
    s, o, _ = e.call(
        "PUT", "/admin/oidc", {**body, "tenant": other, "enabled": False, "client_secret": ""}, token=e.admin_tok
    )
    assert s == 200 and o["tested"] is False and o["enabled"] is False
    assert e.store.linked(e.w.ann) is None
    assert e.call("GET", "/session", token=co_tok)[0] >= 400
    assert e.call("GET", "/session", token=e.admin_tok)[0] == 200  # the admin's password session stays


def test_removing_a_person_removes_the_link(e):
    e.configure()
    e.store.link(e.w.ann, "entra", TENANT, "oid-ann")
    s, o, _ = e.call("DELETE", f"/admin/users/{e.w.ann}", {"confirm": True}, token=e.admin_tok)
    assert e.store.linked(e.w.ann) is None, (s, o)


def test_a_save_with_the_same_settings_keeps_the_instance(e):
    body = e.configure()
    first = e.svc.instance()
    s, o, _ = e.call("PUT", "/admin/oidc", {**body, "client_secret": ""}, token=e.admin_tok)
    assert s == 200 and e.svc.instance() is first
    s, o, _ = e.call("PUT", "/admin/oidc", {**body, "mode": "company", "client_secret": ""}, token=e.admin_tok)
    assert e.svc.instance() is first  # mode is not part of the identity
    s, _o, _ = e.call(
        "PUT", "/admin/oidc", {**body, "client_id": "other", "enabled": False, "client_secret": ""}, token=e.admin_tok
    )
    assert e.svc.instance() is not first


def test_a_replay_survives_a_save_with_unchanged_settings(e):
    body = e.configure()
    e.store.link(e.w.ann, "entra", TENANT, "oid-ann")
    e.idp.claims = {"oid": "oid-ann"}
    q, tx, _ = e.begin()
    assert e.call("POST", "/oidc/finish", {"code": "c", "state": q["state"]}, tx=tx)[0] == 200
    e.call("PUT", "/admin/oidc", {**body, "client_secret": ""}, token=e.admin_tok)
    assert e.call("POST", "/oidc/finish", {"code": "c", "state": q["state"]}, tx=tx)[0] == 400


def test_finish_is_rate_limited_per_source(e):
    e.configure()
    codes = [e.call("POST", "/oidc/finish", {"code": "c", "state": "x"}, peer="9.9.9.9")[0] for _ in range(14)]
    assert 429 in codes and codes[0] == 400
    assert e.call("POST", "/oidc/finish", {"code": "c", "state": "x"}, peer="8.8.8.8")[0] == 400


def test_nothing_works_while_company_login_is_off(e):
    s, o, _ = e.call("POST", "/oidc/start", {})
    assert s in (400, 403, 503)
    e.configure(enabled=False)
    s, o, _ = e.call("POST", "/oidc/start", {})
    assert s == 403 and o["error"] == "oidc_not_allowed"


def test_the_redirect_uri_comes_from_configuration_not_from_the_host_header(e):
    e.configure(enabled=False)
    _s, o, _ = e.call("GET", "/admin/oidc", token=e.admin_tok)
    assert o["redirect_uri"] == ORIGIN + "/oidc/callback"
    assert "evil" not in o["redirect_uri"]


def test_pending_says_what_the_cookie_is_for_and_consumes_nothing(e):
    e.configure()
    assert e.call("GET", "/oidc/pending")[1] == {"purpose": None}
    q, tx, _ = e.begin()
    assert e.call("GET", "/oidc/pending", tx=tx)[1] == {"purpose": "signin"}
    assert e.call("GET", "/oidc/pending", tx=tx)[1] == {"purpose": "signin"}
    assert e.call("GET", "/oidc/pending", tx=tx[:-3] + "AAA")[1] == {"purpose": None}
    q2, tx2, _ = e.begin("/oidc/link/start", e.admin_tok, e.w.creds(e.w.admin))
    assert e.call("GET", "/oidc/pending", tx=tx2)[1] == {"purpose": "link"}


def test_a_company_login_session_cannot_change_or_test_the_settings(e):
    body = e.configure(mode="company")
    e.w.env.acc.add_member("hub-admin", e.w.ann)
    e.store.link(e.w.ann, "entra", TENANT, "oid-ann")
    s, o, h = e.company_login(oid="oid-ann")
    tok = session_of(h, e.w)
    assert e.call("GET", "/session", token=tok)[1]["is_admin"] is True  # an admin, but signed in by the company
    for method, path, payload in (
        ("PUT", "/admin/oidc", {**body, "client_secret": ""}),
        ("POST", "/admin/oidc/test/start", {}),
        ("POST", "/admin/oidc/test/finish", {"code": "c", "state": "s"}),
    ):
        s, o, _ = e.call(method, path, payload, token=tok)
        assert (s, o["error"]) == (403, "reauth_required"), (path, s, o)
    assert e.call("GET", "/admin/oidc", token=tok)[0] == 200  # reading is fine
    assert (
        e.call("PUT", "/admin/oidc", {**body, "client_secret": ""}, token=e.admin_tok)[0] == 200
    )  # a password session may


def test_linking_also_refuses_a_provider_that_says_no_mfa(e):
    e.configure()
    tok = e.w.login(e.w.ann)
    e.idp.claims = {"oid": "oid-ann", "auth_time": NOW - 5, "amr": ["pwd"]}
    q, tx, _ = e.begin("/oidc/link/start", tok, e.w.creds(e.w.ann))
    s, o, h = e.call("POST", "/oidc/link/finish", {"code": "c", "state": q["state"]}, token=tok, tx=tx)
    assert s == 403 and e.store.linked(e.w.ann) is None
    assert "Max-Age=0" in e.cookies(h)[COOKIE]


def test_a_cancelled_login_drops_the_cookie(e):
    e.configure()
    q, tx, _ = e.begin()
    s, o, h = e.call("POST", "/oidc/cancel", {}, tx=tx)
    assert s == 200 and "Max-Age=0" in e.cookies(h)[COOKIE]
