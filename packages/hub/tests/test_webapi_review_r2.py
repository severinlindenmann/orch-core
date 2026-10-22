"""Opus delta review of D13 round 2 (l2-c3, hub#29 5779c79). Strict xfail = an open finding; drop the marker when fixed."""

from __future__ import annotations

import pytest
from test_webapi_core import sign_in_body
from webapi_helpers import Web

from orch_hub.webapi import api as A


@pytest.fixture
def w(tmp_path):
    web = Web(tmp_path)
    yield web
    web.close()


def _try(w, user, peer, totp="000000", password="x" * 12):
    body = {"user_id": user, "password": password, "totp": totp, "client": "app"}
    return w.call("POST", "/session", body, origin=None, peer=peer)


def test_r2_a_few_strangers_cannot_hold_the_global_sign_in_bucket(w):
    assert not hasattr(A, "SIGNIN_GLOBAL_LIMIT")  # no hub-wide bucket: the per-source caps and the semaphore bind
    for i in range(15):
        for _ in range(20):
            _try(w, "0" * 32, f"203.0.113.{i + 1}")
    s, o, _ = w.call("POST", "/session", sign_in_body(w, w.admin, client="app"), origin=None, peer="198.51.100.7")
    assert s == 200, o


def test_r2_one_ipv6_host_is_one_source(w):
    codes = [_try(w, "0" * 32, f"2001:db8::{i:x}")[1].get("error") for i in range(1, 25)]
    assert "rate_limited" in codes


def test_r2_totp_guessing_with_a_known_password_is_bounded_per_user(w):
    codes = []
    for i in range(8):  # 8 sources x 4 guesses = 32 TOTP guesses; before round 2 it was 5 per user per 15 min
        for _ in range(4):
            codes.append(_try(w, w.ann, f"203.0.113.{i + 1}", password="correct horse battery")[1].get("error"))
    # bounded per user (orchestrator, 22 Oct: the lock must not be an oracle, so it answers bad_credentials too)
    assert set(codes) == {"bad_credentials"}
    s, o, _ = w.call("POST", "/session", sign_in_body(w, w.ann, client="app"), origin=None, peer="192.0.2.99")
    assert (s, o) == (401, {"error": "bad_credentials"}), "a correct TOTP is refused while the per-user lock holds"


def test_r2_chained_trusted_proxies_yield_the_client(w):
    w.api.d.trusted_peers = frozenset({"10.0.0.1", "10.0.0.2"})
    src = w.api._source({"x-forwarded-for": "198.51.100.7, 10.0.0.2"}, "10.0.0.1")
    assert src == "198.51.100.7"


def test_r2_xff_from_an_untrusted_peer_or_spoofed_first_entry_is_ignored(w):
    """Holds: a direct peer is its own source; through the edge only the entry the edge appended counts."""
    w.api.d.trusted_peers = frozenset({"10.0.0.1"})
    assert w.api._source({"x-forwarded-for": "1.2.3.4"}, "203.0.113.5") == "203.0.113.5"
    assert w.api._source({"x-forwarded-for": "1.2.3.4, 198.51.100.7"}, "10.0.0.1") == "198.51.100.7"
    assert w.api._source({"x-forwarded-for": "not-an-ip"}, "10.0.0.1") == "10.0.0.1"


def test_r2_get_prepared_is_the_owners_only(w):
    """Holds: another user's id and an unknown id give the same answer; the read does not consume it."""
    s, o, _ = w.call("GET", "/sign/prepared/" + "a" * 32, token=w.login(w.ann))
    assert (s, o) == (404, {"error": "unknown_prepared"})
    s, o, _ = w.call("GET", "/sign/prepared/" + "a" * 32, token=w.login(w.admin))
    assert (s, o) == (404, {"error": "unknown_prepared"})


def test_r3_ipv6_normalised_and_v4_mapped(w):
    assert w.api._source({}, "2001:db8:0:1::5") == w.api._source({}, "2001:db8:0:1:ffff::1") == "2001:db8:0:1::/64"
    assert w.api._source({}, "::ffff:203.0.113.9") == "203.0.113.9"
    assert w.api._source({}, "203.0.113.9") == "203.0.113.9"


def test_r3_xff_ipv6_client_through_edge_is_its_64(w):
    w.api.d.trusted_peers = frozenset({"10.0.0.1"})
    assert w.api._source({"x-forwarded-for": "2001:db8:0:1::7"}, "10.0.0.1") == "2001:db8:0:1::/64"


def test_r3_totp_lock_is_per_user_and_not_an_oracle(w):
    """10 'password right, TOTP wrong' from different sources trip the lock; afterwards the answer is the same
    bad_credentials as for a wrong password, even for fully correct credentials. Time-out and unlock end it."""
    codes = [_try(w, w.ann, f"198.51.100.{i + 1}", password="correct horse battery")[1].get("error") for i in range(12)]
    assert codes == ["bad_credentials"] * 12  # never a distinct code, tripped or not
    s, o, _ = w.call("POST", "/session", sign_in_body(w, w.ann, client="app"), origin=None, peer="192.0.2.77")
    assert (s, o) == (401, {"error": "bad_credentials"})
    w.api.d.channels.unlock(w.ann)
    s, o, _ = w.call("POST", "/session", sign_in_body(w, w.ann, client="app"), origin=None, peer="192.0.2.78")
    assert s == 200, o


def test_r3_wrong_password_and_tripped_lock_answer_identically(w):
    wrong = _try(w, w.ann, "203.0.113.1", password="wrong wrong wrong")
    for i in range(10):
        _try(w, w.ann, f"198.51.100.{i + 1}", password="correct horse battery")
    tripped = _try(w, w.ann, "203.0.113.2", password="correct horse battery")
    assert wrong[0] == tripped[0] == 401 and wrong[1] == tripped[1]


def _clock(w):
    ch = w.api.d.channels
    t = [ch._now()]
    ch._now = lambda: t[0]
    w.api.d.now_ms = lambda: t[0]
    return t


def test_r3_password_backoff_is_per_user_and_source(w):
    t = _clock(w)
    for _ in range(3):  # 3 free tries, then a delay for this source
        _try(w, w.ann, "203.0.113.1", password="wrong wrong wrong")
        t[0] += 1
    s, o, _ = w.call("POST", "/session", sign_in_body(w, w.ann, client="app"), origin=None, peer="203.0.113.1")
    assert (s, o) == (401, {"error": "bad_credentials"})  # inside the backoff, same generic answer
    t[0] += 61_000
    s, o, _ = w.call("POST", "/session", sign_in_body(w, w.ann, client="app"), origin=None, peer="203.0.113.1")
    assert s == 200, o


def test_r3_stranger_without_the_password_cannot_delay_or_lock_the_user_from_another_source(w):
    _clock(w)
    for i in range(60):  # a distributed password spray at one user from many sources
        _try(w, w.ann, f"203.0.113.{i + 1}", password="wrong wrong wrong")
    s, o, _ = w.call("POST", "/session", sign_in_body(w, w.ann, client="app"), origin=None, peer="198.51.100.200")
    assert s == 200, o


def test_r3_ipv6_neighbour_64_cannot_exhaust_ours_but_the_48_is_capped(w, monkeypatch):
    monkeypatch.setattr(A, "SIGNIN_NET_LIMIT", (30, 60_000))
    for i in range(1, 20):  # 19 other /64s inside the same /48, a few tries each
        for j in range(2):
            _try(w, "0" * 32, f"2001:db8:0:{i:x}::{j + 1}")
    # the neighbours did not touch the bucket of a /64 outside this /48 (or our own /64)
    assert _try(w, "0" * 32, "2001:db9:0:1::1")[1].get("error") == "bad_credentials"
    codes = [_try(w, "0" * 32, f"2001:db8:0:{i:x}::9")[1].get("error") for i in range(20, 40)]
    assert "rate_limited" in codes  # the /48 aggregate binds a spray across /64s


def test_r3_source_is_never_empty(w):
    w.api.d.trusted_peers = frozenset({"10.0.0.1"})
    assert w.api._source({"x-forwarded-for": ""}, "10.0.0.1") == "10.0.0.1"
    assert w.api._source({"x-forwarded-for": "1.2.3.4, "}, "10.0.0.1") == "10.0.0.1"
    assert w.api._source({}, "10.0.0.1") == "10.0.0.1"
    assert w.api._source({}, "") == "unknown"


def test_r3_behind_the_edge_needs_trusted_peers(w):
    import dataclasses

    with pytest.raises(ValueError):
        A.WebApi(dataclasses.replace(w.api.d, behind_edge=True, trusted_peers=frozenset()))
    A.WebApi(dataclasses.replace(w.api.d, behind_edge=True, trusted_peers=frozenset({"10.0.0.1"})))


def test_r3_failed_add_user_leaves_no_orphan_uk(w, monkeypatch):
    import os

    from orch_hub.access import AccessError

    ukdir = w.env.keys.state / "uk"
    before = set(os.listdir(ukdir)) if ukdir.exists() else set()
    certs = w.env.keys._gen_dir() / "uk"
    certs_before = set(os.listdir(certs)) if certs.exists() else set()

    def boom(*a, **k):
        raise AccessError("store_error", "OperationalError")

    monkeypatch.setattr(w.api.d.access, "add_user", boom)
    s, _o, _ = w.call("POST", "/admin/users", {"name": "newbie"}, token=w.login(w.admin))
    assert s == 503 and set(os.listdir(ukdir)) == before
    assert (set(os.listdir(certs)) if certs.exists() else set()) == certs_before
