# ruff: noqa: RUF059
"""HUB-PE-04 follow-up: an invitation that is redeemed with the company login (session only, no password, no key)."""

from __future__ import annotations

import json
import sqlite3
import threading

import pytest
from oidc_fake_idp import NOW, TENANT
from test_oidc_api import COOKIE, Env, session_of

from orch_hub.invites import InviteError, Invites
from orch_hub.oidc import PLAIN


@pytest.fixture
def e(tmp_path):
    x = Env(tmp_path)
    inv = Invites(str(tmp_path / "hub" / "invites.db"), now_ms=x.w.clock)
    x.w.api.d.user_invites = inv
    x.inv = inv
    yield x
    inv.close()
    x.close()


def make_invite(e, name="Julia Meier", company=True):
    s, o, _ = e.call(
        "POST", "/admin/invites", {"name": name, **({"company": True} if company else {})}, token=e.admin_tok
    )
    assert s == 200, o
    return o["link"].split("#invite=", 1)[1], o["invite_id"]


def begin(e, token):
    s, o, h = e.call("POST", "/oidc/invite/start", {"token": token})
    return s, o, h


def go(e, token, **claims):
    """Start and finish a company invitation login. Returns (status, body, headers, cookie, state)."""
    e.idp.claims = claims
    s, o, h = begin(e, token)
    assert s == 200, o
    from urllib.parse import parse_qs, urlsplit

    q = {k: v[0] for k, v in parse_qs(urlsplit(o["url"]).query).items()}
    e.idp.nonce, e.idp.challenge = q["nonce"], q["code_challenge"]
    tx = e.cookies(h)[COOKIE].split(";")[0].split("=", 1)[1]
    s, o, h = e.call("POST", "/oidc/invite/finish", {"code": "c", "state": q["state"]}, tx=tx)
    return s, o, h, tx, q["state"]


def users(e):
    return {u["user_id"] for u in e.w.env.acc.users()}


def test_happy_path_session_only_bare_member_named_by_the_invite(e):
    e.configure()
    token, iid = make_invite(e)
    s, peek, _ = e.call("POST", "/invite/peek", {"token": token})
    assert peek == {"kind": "join", "name": "Julia Meier", "needs_proof": False, "company": True, "provider": "entra"}
    s, o, h = begin(e, token)
    assert set(o) == {"url"}  # nothing beyond the address

    assert token not in o["url"] and token[:12] not in o["url"]  # no token in the round trip
    before = users(e)
    s, o, h, tx, state = go(e, token, oid="oid-julia", name="Mallory Admin", email="julia@acme.example")
    assert s == 200 and o["name"] == "Julia Meier"
    uid = o["user_id"]
    assert users(e) - before == {uid}
    assert list(e.w.env.acc.groups_of(uid)) == [] and not e.w.env.acc.is_hub_admin(uid)
    assert e.store.user_for("entra", TENANT, "oid-julia") == uid
    tok = session_of(h, e.w)
    me = e.call("GET", "/session", token=tok)[1]
    assert me["company_login"] is True and me["name"] == "Julia Meier"  # the invite's name wins
    assert "Max-Age=0" in e.cookies(h)[COOKIE]
    # the admin sees what the company said, and that the names differ
    s, st, _ = e.call("GET", f"/admin/invites/{iid}", token=e.admin_tok)
    assert st["state"] == "joined" and st["company_email"] == "julia@acme.example"
    assert st["company_name"] == "Mallory Admin" and st["name_differs"] is True and st["name"] == "Julia Meier"
    # audit: no email, token or code in any record
    rows = json.dumps(e.log.list())
    types = [r["type"] for r in e.log.list()]
    assert "invite.redeemed" in types and "oidc.linked" in types
    assert "julia@acme" not in rows and token not in rows
    # decision C: no key can be made from this session
    from channels_helpers import new_channel

    from orch_hub.contract.canon import b64u

    key = new_channel()
    s, o, _ = e.call(
        "POST",
        "/channels/register/begin",
        {"kind": "browser", "key_pub": b64u(key.public), "password": "x", "totp": "123456"},
        token=tok,
    )
    assert s in (400, 401, 403)
    assert e.w.env.svc.db.q("SELECT COUNT(*) FROM channels WHERE user_id=?", (uid,))[0][0] == 0


def test_a_company_invite_never_redeems_with_a_password_or_a_proof(e):
    e.configure()
    token, _ = make_invite(e)
    before = users(e)
    for body in (
        {"token": token, "password": "a long enough password"},
        {
            "token": token,
            "password": "a long enough password",
            "proof": {"channel_id": "x", "challenge": "y", "sig": "z"},
        },
    ):
        s, o, _ = e.call("POST", "/invite/redeem", body)
        assert (s, o["error"]) == (403, "bad_invite")
    assert users(e) == before
    assert go(e, token, oid="oid-julia")[0] == 200  # the invite was not burned by the refusals


def test_start_is_checked_like_a_sign_in(e):
    e.configure()
    plain, _ = make_invite(e, "Plain Pat", company=False)
    s, o, _ = e.call("POST", "/oidc/invite/start", {"token": plain})
    assert (s, o["error"]) == (403, "bad_invite")
    s, o, _ = e.call("POST", "/oidc/invite/start", {"token": "x" * 40})
    assert (s, o["error"]) == (403, "bad_invite")
    # no browser origin: refused before anything is made
    token, _ = make_invite(e)
    s, hdrs, out = e.w.api.handle(
        "POST",
        "/api/v1/oidc/invite/start",
        {"host": "127.0.0.1:9000", "x-orch-csrf": "1", "content-type": "application/json"},
        json.dumps({"token": token}).encode(),
        "",
    )
    assert s == 403
    # the per-source start limiter applies (burst 10)
    codes = [e.call("POST", "/oidc/invite/start", {"token": token}, peer="7.7.7.7")[0] for _ in range(14)]
    assert 429 in codes


def test_company_invite_needs_company_login_on_and_tested(e):
    s, o, _ = e.call("POST", "/admin/invites", {"name": "Nobody", "company": True}, token=e.admin_tok)
    assert (s, o["error"]) == (409, "oidc_not_ready")  # nothing configured
    e.configure(enabled=False)
    s, o, _ = e.call("POST", "/admin/invites", {"name": "Nobody", "company": True}, token=e.admin_tok)
    assert (s, o["error"]) == (409, "oidc_not_ready")  # configured, not on
    assert e.call("POST", "/admin/invites", {"name": "x", "company": "yes"}, token=e.admin_tok)[0] == 400
    assert e.call("POST", "/admin/invites", {"name": "x", "colour": 1}, token=e.admin_tok)[0] == 400


def test_cookies_of_other_purposes_do_not_cross(e):
    e.configure()
    token, iid = make_invite(e)
    e.store.link(e.w.ann, "entra", TENANT, "oid-ann")
    # an invite cookie cannot sign in
    e.idp.claims = {"oid": "oid-ann"}
    s, o, h = begin(e, token)
    from urllib.parse import parse_qs, urlsplit

    q = {k: v[0] for k, v in parse_qs(urlsplit(o["url"]).query).items()}
    e.idp.nonce, e.idp.challenge = q["nonce"], q["code_challenge"]
    tx = e.cookies(h)[COOKIE].split(";")[0].split("=", 1)[1]
    assert e.call("POST", "/oidc/finish", {"code": "c", "state": q["state"]}, tx=tx)[0] == 400
    ann = e.w.login(e.w.ann)
    assert e.call("POST", "/oidc/link/finish", {"code": "c", "state": q["state"]}, token=ann, tx=tx)[0] == 400
    # a sign-in cookie cannot finish an invite; the invite stays unused
    q2, tx2, _ = e.begin()
    s, o, _ = e.call("POST", "/oidc/invite/finish", {"code": "c", "state": q2["state"]}, tx=tx2)
    assert s == 400
    assert e.call("GET", f"/admin/invites/{iid}", token=e.admin_tok)[1]["state"] == "waiting"


def test_finish_has_no_wildcard_bind():
    """The fake IdP would ACCEPT every one of these exchanges (right nonce, no PKCE check): only the purpose and bind rules refuse."""
    from urllib.parse import parse_qs, urlsplit

    from oidc_fake_idp import FakeIdp, entra_cfg

    from orch_hub.oidc import Oidc, OidcError

    idp = FakeIdp(entra_cfg())
    o = Oidc(entra_cfg(), idp, b"k" * 32, lambda: NOW, frozenset({"idp.example"}))
    idp.claims = {"oid": "oid-x"}

    def started(purpose, bind):
        url, cookie = o.start(source="s", purpose=purpose, bind=bind)
        q = {k: v[0] for k, v in parse_qs(urlsplit(url).query).items()}
        idp.nonce_of[q["nonce"]] = q["nonce"]  # the code IS the nonce: each login is answered with its own
        return q["state"], cookie, q["nonce"]

    st, cookie, n1 = started("invite", "a" * 64)
    for kw in (
        {"purpose": "invite", "bind": "a" * 64},
        {"purpose": "invite", "bind": None},
        {"purpose": "invite"},
        {"bind": "a" * 64},
    ):
        with pytest.raises(OidcError) as err:
            o.finish(cookie, st, n1, "S", "s", **kw)  # finish() never finishes an invite login, with or without a bind
        assert err.value.code == "bad_state", kw
    # a non-str bind is refused on an ordinary login too (no "any bind")
    st2, cookie2, n2 = started("signin", "")
    for bad in (None, 0, b"", ["x"]):
        with pytest.raises(OidcError) as err:
            o.finish(cookie2, st2, n2, "S", "s", bind=bad)
        assert err.value.code == "bad_state", bad
    # the same cookies DO finish through their own door, so the refusals above were the rules, not a failing exchange
    ident, bind = o.finish_invite(cookie, st, n1, "S", "s")
    assert ident.subject == "oid-x" and bind == "a" * 64
    assert o.finish(cookie2, st2, n2, "S", "s").subject == "oid-x"
    # and finish_invite takes no other purpose
    st3, cookie3, n3 = started("signin", "")
    with pytest.raises(OidcError) as err:
        o.finish_invite(cookie3, st3, n3, "S", "s")
    assert err.value.code == "bad_state"


def start_login(e, token, oid):
    """A started company login for this invite, not yet finished: (state, cookie). The code carries its nonce for the fake IdP."""
    from urllib.parse import parse_qs, urlsplit

    s, o, h = begin(e, token)
    assert s == 200, o
    q = {k: v[0] for k, v in parse_qs(urlsplit(o["url"]).query).items()}
    e.idp.nonce_of[q["nonce"]] = q["nonce"]
    e.idp.oid_of[q["nonce"]] = oid
    return q, e.cookies(h)[COOKIE].split(";")[0].split("=", 1)[1]


def finish_login(e, q, tx):
    return e.call("POST", "/oidc/invite/finish", {"code": q["nonce"], "state": q["state"]}, tx=tx)


def test_replay_and_two_logins_for_one_invite_make_one_person(e):
    e.configure()
    token, _ = make_invite(e)
    e.idp.challenge = None
    first, second = start_login(e, token, "oid-a"), start_login(e, token, "oid-b")
    before = users(e)
    s, o, _ = finish_login(e, *first)
    assert s == 200
    assert finish_login(e, *first)[0] == 400  # the cookie is spent
    s2, o2, _ = finish_login(e, *second)  # a second login for the same, now used, invite
    assert (s2, o2["error"]) == (403, "bad_invite")
    assert len(users(e) - before) == 1 and e.store.user_for("entra", TENANT, "oid-b") is None


def test_parallel_finishes_of_one_invite_make_exactly_one_person(e):
    e.configure()
    token, _ = make_invite(e)
    e.idp.challenge = None
    logins = [start_login(e, token, f"oid-{i}") for i in range(6)]
    before, results, gate = users(e), [], threading.Barrier(6)

    def run(i):
        gate.wait()
        results.append(finish_login(e, *logins[i])[0])

    ts = [threading.Thread(target=run, args=(i,)) for i in range(6)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert sorted(results) == [200] + [403] * 5
    assert len(users(e) - before) == 1


def test_an_idp_failure_does_not_burn_the_invite(e):
    e.configure()
    token, iid = make_invite(e)
    e.idp.claims = {"oid": "oid-julia"}
    s, o, h = begin(e, token)
    from urllib.parse import parse_qs, urlsplit

    q = {k: v[0] for k, v in parse_qs(urlsplit(o["url"]).query).items()}
    e.idp.nonce, e.idp.challenge = q["nonce"], "wrong-challenge"  # the IdP refuses (PKCE)
    tx = e.cookies(h)[COOKIE].split(";")[0].split("=", 1)[1]
    s, o, _ = e.call("POST", "/oidc/invite/finish", {"code": "c", "state": q["state"]}, tx=tx)
    assert s == 400 and e.call("GET", f"/admin/invites/{iid}", token=e.admin_tok)[1]["state"] == "waiting"
    for claims in ({"tid": "99999999-2222-3333-4444-555555555555"}, {"amr": ["pwd"]}):  # restrictions still apply
        e.idp.challenge = None
        s, o, h, _, _ = go(e, token, oid="oid-julia", **claims)
        assert s in (400, 403)
        assert e.call("GET", f"/admin/invites/{iid}", token=e.admin_tok)[1]["state"] == "waiting"
    assert go(e, token, oid="oid-julia")[0] == 200


def test_an_identity_already_linked_elsewhere_is_refused_and_the_invite_stays(e):
    e.configure()
    token, iid = make_invite(e)
    e.store.link(e.w.ann, "entra", TENANT, "oid-ann")
    before = users(e)
    s, o, h, _, _ = go(e, token, oid="oid-ann")
    assert (s, o["error"]) == (409, "oidc_already_linked") and o["message"] == PLAIN["already_linked"]
    assert users(e) == before and e.store.user_for("entra", TENANT, "oid-ann") == e.w.ann
    assert e.call("GET", f"/admin/invites/{iid}", token=e.admin_tok)[1]["state"] == "waiting"
    assert go(e, token, oid="oid-julia")[0] == 200


def test_a_failure_inside_the_claim_undoes_the_person_and_keeps_the_invite(e, monkeypatch):
    e.configure()
    token, iid = make_invite(e)
    before = users(e)
    real = e.store.link

    def racing_link(uid, *a):
        real("someone-else", *a)  # another login took the identity between the pre-check and the insert
        return real(uid, *a)

    keys = e.w.env.keys
    key_dirs = [keys.state / "uk", keys._gen_dir() / "uk"]
    files = lambda: {str(p) for d in key_dirs for p in d.iterdir()}
    files_before = files()
    monkeypatch.setattr(e.store, "link", racing_link)
    s, o, h, _, _ = go(e, token, oid="oid-julia")
    assert (s, o["error"]) == (409, "oidc_already_linked")
    assert users(e) == before  # the half-made person was removed
    assert files() == files_before  # and so were uk/<uid>.key and the generation's uk/<uid>.json
    assert e.call("GET", f"/admin/invites/{iid}", token=e.admin_tok)[1]["state"] == "waiting"


def test_an_invite_voided_between_start_and_finish_gives_the_generic_answer(e):
    e.configure()
    token, _ = make_invite(e)
    e.idp.claims = {"oid": "oid-julia"}
    s, o, h = begin(e, token)
    from urllib.parse import parse_qs, urlsplit

    q = {k: v[0] for k, v in parse_qs(urlsplit(o["url"]).query).items()}
    e.idp.nonce, e.idp.challenge = q["nonce"], q["code_challenge"]
    tx = e.cookies(h)[COOKIE].split(";")[0].split("=", 1)[1]
    e.inv.void_created_by(e.w.admin)
    before = users(e)
    s, o, _ = e.call("POST", "/oidc/invite/finish", {"code": "c", "state": q["state"]}, tx=tx)
    assert (s, o["error"]) == (403, "bad_invite") and users(e) == before


def test_a_provider_change_voids_unused_company_invites(e):
    body = e.configure()
    token, _ = make_invite(e)
    plain, _ = make_invite(e, "Plain Pat", company=False)
    other = "99999999-2222-3333-4444-555555555555"
    s, o, _ = e.call(
        "PUT", "/admin/oidc", {**body, "tenant": other, "enabled": False, "client_secret": ""}, token=e.admin_tok
    )
    assert s == 200
    assert e.call("POST", "/invite/peek", {"token": token})[0] == 403  # the company link is dead
    assert e.call("POST", "/invite/peek", {"token": plain})[0] == 200  # a password link is not touched


def test_labels_are_printable_and_capped(e):
    e.configure()
    token, iid = make_invite(e)
    go(e, token, oid="oid-julia", email="ju\x00lia\n" + "a" * 400 + "@x.example", name="<b>Eve</b>\x07")
    st = e.call("GET", f"/admin/invites/{iid}", token=e.admin_tok)[1]
    assert len(st["company_email"]) <= 200 and all(ch.isprintable() for ch in st["company_email"])
    assert all(ch.isprintable() for ch in st["company_name"])
    # a normal password-redeemed link is unchanged
    assert (
        "company_email"
        not in e.call("GET", f"/admin/invites/{make_invite(e, 'Plain', company=False)[1]}", token=e.admin_tok)[1]
    )


def test_an_older_invites_store_gets_the_new_columns(tmp_path):
    p = tmp_path / "old.db"
    with sqlite3.connect(p) as c:
        c.execute(
            "CREATE TABLE invites (id TEXT PRIMARY KEY, token_hash TEXT NOT NULL UNIQUE, kind TEXT NOT NULL CHECK (kind IN ('join','reset','recovery')), for_user TEXT, name TEXT, created_by TEXT NOT NULL, created_ms INTEGER NOT NULL, expires_ms INTEGER NOT NULL, used_ms INTEGER, redeemed_user TEXT)"
        )
    p.chmod(0o600)
    inv = Invites(str(p))
    try:
        token, iid, _ = inv.create("join", created_by="a", name="n", company=True)
        assert inv.peek(token).company is True
        assert inv.status(iid, "a") == {"state": "waiting", "company": True}
        with pytest.raises(InviteError):
            inv.create("reset", created_by="a", for_user="u", company=True)
    finally:
        inv.close()


def test_the_people_list_says_who_joined_with_which_company_account(e):
    e.configure()
    token, _ = make_invite(e)
    plain, _ = make_invite(e, "Plain Pat", company=False)
    s, o, *_ = go(e, token, oid="oid-julia", name="Mallory Admin", email="julia@acme.example")
    uid = o["user_id"]
    s, body, _ = e.call("GET", "/admin/users", token=e.admin_tok)
    by = {u["user_id"]: u for u in body["users"]}
    assert by[uid]["company"] == {"email": "julia@acme.example", "name": "Mallory Admin"}
    assert "company" not in by[e.w.ann] and "company" not in by[e.w.admin]
    assert e.call("GET", "/admin/users", token=e.w.login(e.w.ann))[0] == 403  # admins only


def test_no_session_after_the_claim_gives_the_plain_answer_and_clears_the_cookie(e, monkeypatch):
    from orch_hub.channels.service import ChannelError

    e.configure()
    token, iid = make_invite(e)

    def boom(uid):
        raise ChannelError("store_error")

    monkeypatch.setattr(e.w.env.svc, "open_company_session", boom)
    s, o, h, _, _ = go(e, token, oid="oid-julia")
    assert (s, o["error"]) == (403, "oidc_not_allowed") and "Max-Age=0" in e.cookies(h)[COOKIE]
    assert e.w.api._cookie_name not in e.cookies(h)  # no session cookie
