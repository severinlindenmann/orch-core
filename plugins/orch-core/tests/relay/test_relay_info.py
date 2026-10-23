"""H12d-3: ``GET /v2/relay-info`` (orch-v2-host §0.8.6 item 9.8), the relay's reader level, read at link step 1
(:func:`orch.relay.session.fetch_relay_info`) and at every R1 login (the ``relay_info`` hook of :class:`Session`).

The level is an honest-operator statement (TLS to the stored origin, never a security control): an absent route
(404) is level 0, a malformed answer is :class:`RelayMisbehaving` and never a level, and a failed read at login keeps
the last stored level (the login itself is not refused)."""

from __future__ import annotations

import pytest

from orch.net import http
from orch.relay import retry
from orch.relay.session import RelayInfo, RelayMisbehaving, fetch_relay_info

from .helpers import OPEN_CONNS, V_ORIGIN
from .helpers import session as make_session
from .mock import Expect, MockRelay, fixture_expect, json_reply
from .test_session import login_expects


@pytest.fixture
def relay():
    m = MockRelay()
    yield m
    assert m.errors == []
    m.close()


def conn(relay) -> http.RelayConn:
    c = http.RelayConn(relay.origin())
    OPEN_CONNS.append(c)
    return c


def info_expect(status: int = 200, obj=None) -> Expect:
    return Expect("GET", "/v2/relay-info", {}, {}, None, json_reply(status, obj if obj is not None else {}))


def test_the_fixture_success_is_level_1_with_the_publication_cap(relay):
    relay.expect(fixture_expect("relay_info", "success"))
    assert fetch_relay_info(conn(relay)) == RelayInfo(reader=1, max_publication_bytes=16777216)


def test_an_absent_route_is_level_0(relay):
    relay.expect(info_expect(404, {"error": "not_found"}))
    assert fetch_relay_info(conn(relay)) == RelayInfo(reader=0, max_publication_bytes=None)


@pytest.mark.parametrize("name", ["refusal_400_malformed", "refusal_429_rate_limited"])
def test_the_fixture_refusals_are_relay_refusals_never_a_level(relay, name):
    relay.expect(fixture_expect("relay_info", name, body=None))
    with pytest.raises(retry.RelayRefused):
        fetch_relay_info(conn(relay))


@pytest.mark.parametrize(
    "obj",
    [
        {"v": 1},
        {"reader": 1},
        {"v": 2, "reader": 1},
        {"v": 1, "reader": "1"},
        {"v": 1, "reader": -1},
        {"v": 1, "reader": True},
        {"v": 1, "reader": 2**31},
        {"v": 1, "reader": 1, "extra": 1},
        {"v": 1, "reader": 1, "max_publication_bytes": 0},
        {"v": 1, "reader": 1, "max_publication_bytes": "1"},
        [],
    ],
)
def test_a_malformed_answer_is_misbehaving_never_a_level(relay, obj):
    relay.expect(info_expect(200, obj))
    with pytest.raises(RelayMisbehaving):
        fetch_relay_info(conn(relay))


def test_a_higher_unknown_level_is_passed_on_as_declared(relay):
    relay.expect(info_expect(200, {"v": 1, "reader": 7}))
    assert fetch_relay_info(conn(relay)).reader == 7


# --- the R1 login hook --------------------------------------------------------------------------------------------


def test_every_login_reads_relay_info_and_reports_the_level(relay):
    seen: list[int] = []
    s = make_session(relay.origin(), V_ORIGIN, relay_info=seen.append)
    relay.expect(*login_expects(), fixture_expect("relay_info", "success"))
    s.login()
    assert seen == [1]
    relay.expect(*login_expects(fresh=True), info_expect(404, {"error": "not_found"}))
    s.login()
    assert seen == [1, 0]  # a relay redeployed without the route: level 0 at the next login


def test_without_the_hook_a_login_sends_nothing_more(relay):
    s = make_session(relay.origin(), V_ORIGIN)
    relay.expect(*login_expects())
    s.login()
    assert [x.path for x in relay.seen][-1] == "/v2/session/login"


@pytest.mark.parametrize("reply", [(429, {"error": "rate_limited"}), (200, {"v": 1, "reader": "x"})])
def test_a_failed_read_at_login_keeps_the_last_level_and_the_login(relay, reply):
    seen: list[int] = []
    s = make_session(relay.origin(), V_ORIGIN, relay_info=seen.append)
    relay.expect(*login_expects(), info_expect(*reply))
    s.login()
    assert seen == [] and s.logged_in


def test_a_hook_that_raises_never_fails_the_login(relay):
    def boom(level: int) -> None:
        raise RuntimeError("hook failed")

    s = make_session(relay.origin(), V_ORIGIN, relay_info=boom)
    relay.expect(*login_expects(), fixture_expect("relay_info", "success"))
    s.login()
    assert s.logged_in
