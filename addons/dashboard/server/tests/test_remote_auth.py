"""#1198 R1-4: the session kind is required, lifetimes, listener binding and the passkey-login buckets."""

# ruff: noqa: F811

import pytest
from conftest import PORT
from remote_kit import (  # noqa: F401
    HTTPS,
    NAME,
    RUN,
    SOURCE,
    Clock,
    app,
    bearer,
    echo,
    front,
    local,
    loop,
    passkey_store,
    remote,
    socket_dir,
    terminals_app,
)

from orch_dashboard import auth as auth_module
from orch_dashboard.auth import (
    LIFETIMES,
    REMOTE_GLOBAL,
    REMOTE_MAX_SOURCES,
    REMOTE_PER_SOURCE,
    Auth,
    client_address,
    source_key,
)
from orch_dashboard.stepup import REVEAL_SESSION_KINDS


def test_exchange_takes_the_kind_and_a_launch_code_opens_a_local_session_only():
    auth = Auth()
    with pytest.raises(TypeError):
        auth.exchange(auth.launch(PORT)["code"])  # no default: every caller says what it is opening
    for kind in ("remote", "hub", "passkey", "", None):
        with pytest.raises(ValueError):
            auth.exchange(auth.launch(PORT)["code"], kind)
    token = auth.exchange(auth.launch(PORT)["code"], "local")[0]["token"]
    assert auth.login_kind(token) == "local" and auth.listener_of(token) == "loopback"


def test_the_remote_kind_is_only_what_the_passkey_login_opens():
    auth = Auth()
    got = auth.open_remote()
    assert auth.login_kind(got["token"]) == "remote" and auth.listener_of(got["token"]) == "remote"
    assert auth.login_kind("unknown") is None and auth.listener_of("unknown") is None
    assert "remote" not in REVEAL_SESSION_KINDS and REVEAL_SESSION_KINDS == {"local"}


def test_remote_sessions_live_12_hours_and_2_idle_and_local_ones_keep_24_and_12():
    assert LIFETIMES == {"local": (86400, 43200), "remote": (43200, 7200)}
    clock = Clock()
    auth = Auth(clock=clock)
    local = auth.exchange(auth.launch(PORT)["code"], "local")[0]
    remote = auth.open_remote()
    assert (
        remote["expires_at"][:13]
        == auth_module.datetime.fromtimestamp(clock.now + 43200, auth_module.UTC).isoformat()[:13]
    )
    assert (
        local["expires_at"][:13]
        == auth_module.datetime.fromtimestamp(clock.now + 86400, auth_module.UTC).isoformat()[:13]
    )
    clock.now += 7200 - 1
    assert auth.valid(remote["token"])  # touched: the idle clock restarts
    clock.now += 7200 - 1
    assert auth.valid(remote["token"]) and auth.valid(local["token"])
    clock.now += 7200  # exactly 2 h without use
    assert not auth.valid(remote["token"]) and auth.valid(local["token"])
    assert auth.login_kind(remote["token"]) is None and auth.listener_of(remote["token"]) is None
    # absolute: a remote session in constant use still ends at 12 h
    busy = auth.open_remote()["token"]
    for _ in range(11):
        clock.now += 3600
        assert auth.valid(busy)
    clock.now += 3600
    assert not auth.valid(busy)


def test_a_local_session_is_unchanged_by_the_remote_lifetimes():
    clock = Clock()
    auth = Auth(clock=clock)
    token = auth.exchange(auth.launch(PORT)["code"], "local")[0]["token"]
    for _ in range(23):
        clock.now += 3600
        assert auth.valid(token)
    clock.now += 3600
    assert not auth.valid(token)  # 24 h absolute


def test_logout_and_log_out_everywhere_end_remote_sessions_only():
    auth = Auth()
    local = auth.exchange(auth.launch(PORT)["code"], "local")[0]["token"]
    a, b, c = (auth.open_remote()["token"] for _ in range(3))
    auth.logout(a)
    assert not auth.valid(a) and auth.valid(b)
    auth.logout_everywhere()
    assert not auth.valid(b) and not auth.valid(c) and auth.valid(local)
    assert auth.listeners.keys() == {local}


def test_sources_are_ipv4_32_and_ipv6_64_and_anything_else_is_refused():
    assert source_key("203.0.113.7") == "203.0.113.7"
    assert source_key("2001:db8:1:2:aaaa:bbbb:cccc:dddd") == source_key("2001:db8:1:2::1") == "2001:db8:1:2::/64"
    assert source_key("2001:db8:1:3::1") != source_key("2001:db8:1:2::1")
    assert source_key("::ffff:203.0.113.7") == "203.0.113.7" == client_address("::ffff:203.0.113.7")
    for bad in (
        "",
        "nope",
        "203.0.113.7, 198.51.100.1",
        "203.0.113.7:80",
        "1.2.3",
        " 1.2.3.4",
        "1.2.3.4\n",
        "１.２.３.４",
        None,
        7,
    ):
        assert client_address(bad) is None and source_key(bad) is None, bad


def test_login_buckets_are_per_source_and_a_refused_try_is_not_counted():
    clock = Clock()
    auth = Auth(clock=clock)
    for _ in range(REMOTE_PER_SOURCE):
        assert auth.remote_attempt(SOURCE)
    assert not auth.remote_attempt(SOURCE) and not auth.remote_attempt("::ffff:" + SOURCE)
    assert auth.remote_attempt("203.0.113.8")  # another source has its own budget (the owner's is not the attacker's)
    assert len(auth.remote_all) == REMOTE_PER_SOURCE + 1  # the refused tries took nothing from the global bucket
    clock.now += 59
    assert not auth.remote_attempt(SOURCE)
    clock.now += 2
    assert auth.remote_attempt(SOURCE)  # a minute later the bucket has drained
    assert not auth.remote_attempt("not an address")


def test_a_whole_ipv6_64_is_one_source():
    auth = Auth(clock=Clock())
    for n in range(REMOTE_PER_SOURCE):
        assert auth.remote_attempt(f"2001:db8:5:6::{n + 1:x}")
    assert not auth.remote_attempt("2001:db8:5:6:ffff::1")
    assert auth.remote_attempt("2001:db8:5:7::1")


def test_the_global_bucket_guards_the_cpu_far_above_one_source():
    assert REMOTE_GLOBAL == 600 and REMOTE_GLOBAL > 10 * REMOTE_PER_SOURCE
    auth = Auth(clock=Clock())
    for n in range(REMOTE_GLOBAL):
        assert auth.remote_attempt(f"198.51.{n // 250}.{n % 250}")
    assert not auth.remote_attempt("192.0.2.1")  # a distributed flood: the global cap holds
    assert len(auth.remote_all) == REMOTE_GLOBAL


def test_internet_traffic_never_uses_the_launch_limiter(monkeypatch):
    auth = Auth(clock=Clock())
    for n in range(3 * REMOTE_PER_SOURCE):
        auth.remote_attempt(f"198.51.100.{n % 200}")
    assert not auth.attempts  # the launch limiter saw nothing
    for _ in range(15):
        auth.exchange("wrong", "local")
    assert auth.exchange(auth.launch(PORT)["code"], "local")[1] in (200, 429)
    # and the other way: a spent launch limiter does not stop a passkey login
    assert auth.remote_attempt("192.0.2.9")


def test_tracked_sources_are_bounded_and_the_overflow_shares_one_bucket():
    auth = Auth(clock=Clock())
    for n in range(REMOTE_MAX_SOURCES):
        auth.remote_sources[f"10.{n >> 16}.{(n >> 8) & 255}.{n & 255}"] = auth_module.deque([auth.clock()])
    for _ in range(REMOTE_PER_SOURCE):
        assert auth.remote_attempt("192.0.2.200")
    assert not auth.remote_attempt("192.0.2.201")  # new sources past the cap share one (stricter) bucket
    assert len(auth.remote_sources) == REMOTE_MAX_SOURCES + 1


def test_the_global_bucket_flood_costs_no_sweep_and_buckets_drain_once_a_second():
    clock = Clock()
    auth = Auth(clock=clock)
    for n in range(REMOTE_GLOBAL):
        assert auth.remote_attempt(f"198.51.{n // 250}.{n % 250}")
    swept = auth.remote_swept
    for _ in range(50):
        assert not auth.remote_attempt("192.0.2.1")
    assert auth.remote_swept == swept  # a refused flood never ran the full sweep
    clock.now += 61
    assert auth.remote_attempt("192.0.2.1") and auth.remote_swept > swept
