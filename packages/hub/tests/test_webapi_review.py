"""Adversarial tests from the Opus security review of D13 (l2-d13, hub#29): one test per finding, now fixed."""

from __future__ import annotations

import os

import pytest
from test_webapi_core import sign_in_body
from webapi_helpers import ORIGIN, Web

from orch_hub.channels import service as CS
from orch_hub.webapi import api as A


@pytest.fixture
def w(tmp_path):
    web = Web(tmp_path)
    yield web
    web.close()


def _bad(w, user, peer="203.0.113.9"):
    return w.call(
        "POST",
        "/session",
        {"user_id": user, "password": "x" * 12, "totp": "000000", "client": "app"},
        origin=None,
        peer=peer,
    )


def test_review_unauthenticated_peer_cannot_lock_out_a_hub_admin(w):
    # user ids are not secret: they are in every custodial event's actor ("u_<id>") that agents read in the workspace
    for _ in range(5):
        assert _bad(w, w.admin)[0] == 401
    # the admin signs in from their own address; the stranger's failures count against the stranger's pair only
    s, o, _ = w.call("POST", "/session", sign_in_body(w, w.admin, client="app"), origin=None, peer="198.51.100.7")
    assert s == 200, o
    # ... while the stranger stays locked out from the stranger's address, even with the right credentials
    s, o, _ = w.call("POST", "/session", sign_in_body(w, w.admin, client="app"), origin=None, peer="203.0.113.9")
    assert (s, o) == (429, {"error": "rate_limited"})


def test_review_sign_in_work_is_capped_across_user_ids(w, monkeypatch):
    calls = []
    real = CS._pw_hash
    monkeypatch.setattr(CS, "_pw_hash", lambda p, s: calls.append(1) or real(p, s))
    codes = [_bad(w, os.urandom(16).hex())[1]["error"] for _ in range(40)]  # 40 fresh ids, one source
    assert "rate_limited" in codes and len(calls) < 40


def test_review_cookie_uses_host_prefix(w):
    s, _, h = w.call("POST", "/session", sign_in_body(w, w.ann), origin=ORIGIN)
    assert s == 200
    assert h["Set-Cookie"].startswith("__Host-") and "Path=/;" in h["Set-Cookie"] + ";"


def test_review_app_token_is_not_accepted_as_a_cookie(w):
    tok = w.login(w.ann, "app")  # a pairing session (bearer)
    s, _, _ = w.call("GET", "/session", token=tok, cookie=True)
    assert s == 401


def test_review_bad_user_name_creates_no_signing_key(w):
    admin = w.login(w.admin)
    ukdir = w.env.keys.state / "uk"
    before = set(os.listdir(ukdir)) if ukdir.exists() else set()
    s, o, _ = w.call("POST", "/admin/invites", {"name": "bad\nname"}, token=admin)
    assert (s, o) == (400, {"error": "malformed"})
    assert set(os.listdir(ukdir)) == before


def test_review_render_escapes_keys_too():
    out = A._render({"a" + chr(0x202E) + "b": "x"})
    assert all(chr(0x202E) not in k for k in out)


def test_review_held_invariants(w):
    """What the review checked and found holding: no-Origin/null-Origin cookie writes refused, Origin on a bearer
    request still checked, revoke of an unknown id is forbidden_role, admin route needs the role, a removed admin's
    session dies."""
    ann = w.login(w.ann)
    for origin in (None, "null", "http://127.0.0.1:9000.evil"):
        assert w.call("DELETE", "/session", token=ann, cookie=True, origin=origin)[:2] == (
            403,
            {"error": "not_allowed"},
        )
    assert w.call("DELETE", "/session", token=ann, origin="null")[:2] == (403, {"error": "not_allowed"})
    assert w.call("POST", "/channels/" + "ab" * 16 + "/revoke", token=ann)[:2] == (403, {"error": "forbidden_role"})
    assert w.call("GET", "/admin/audit", token=ann)[:2] == (403, {"error": "forbidden_role"})
    admin = w.login(w.admin)
    w.env.acc.remove_member("hub-admin", w.admin)
    assert w.call("GET", "/admin/users", token=admin)[:2] == (403, {"error": "forbidden_role"})


def test_forwarded_address_is_trusted_only_from_the_edge_peer(w):
    w.api.d.trusted_peers = frozenset({"127.0.0.1"})
    assert w.api._source({"x-forwarded-for": "9.9.9.9, 203.0.113.5"}, "127.0.0.1") == "203.0.113.5"
    assert w.api._source({"x-forwarded-for": "203.0.113.5"}, "198.51.100.1") == "198.51.100.1"  # not the edge
    assert w.api._source({"x-forwarded-for": "not-an-ip"}, "127.0.0.1") == "127.0.0.1"


def test_sign_in_refuses_instead_of_queueing_when_all_hash_slots_are_busy(w):
    for _ in range(A.SIGNIN_CONCURRENCY):
        assert w.api._hashing.acquire(blocking=False)
    assert _bad(w, w.ann)[1] == {"error": "rate_limited"}


def test_server_answers_503_beyond_its_worker_bound(w):
    import socket

    from orch_hub.webapi.server import WebServer

    srv = WebServer(w.api, "127.0.0.1", 0, max_workers=1)
    srv.start()
    try:
        hold = socket.create_connection(srv.address)  # takes the only slot (no request sent)
        try:
            for _ in range(50):
                with socket.create_connection(srv.address) as c:
                    c.settimeout(3)
                    got = c.recv(100)
                if got.startswith(b"HTTP/1.1 503"):
                    break
            assert got.startswith(b"HTTP/1.1 503")
        finally:
            hold.close()
    finally:
        srv.close()


def test_browser_token_is_not_accepted_as_a_bearer(w):
    tok = w.login(w.ann, "browser")
    assert w.call("GET", "/session", token=tok, cookie=True)[0] == 200
    assert w.call("GET", "/session", token=tok, cookie=False)[0] == 401
