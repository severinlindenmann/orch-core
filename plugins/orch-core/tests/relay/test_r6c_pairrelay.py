"""R6c: the pairing routes against the relay's recorded HTTP fixtures on a loopback mock (11A.10 layer 1): every
success request is byte-exact, and every recorded refusal maps to its :class:`PairRelayError` kind. Never a real
relay."""

from __future__ import annotations

import pytest

from orch.relay.r6.pair import PairRelayError
from orch.relay.r6.pairrelay import SessionPairRelay
from orch.relay.session import Fenced

from . import corpus
from .helpers import V_ORIGIN, WS
from .helpers import session as make_session
from .mock import MockRelay, fixture_expect
from .test_session import login_expects


@pytest.fixture
def relay():
    m = MockRelay()
    yield m
    assert m.errors == []
    m.close()


def pr(relay):
    relay.expect(*login_expects(), fixture_expect("host_hold", "success"))
    s = make_session(relay.origin(), V_ORIGIN)
    s.hold()
    return SessionPairRelay(s, WS)


def req(name, case="success"):
    return corpus.case(name, case)["request"]


def test_post_answer_is_the_fixtures_raw_envelope(relay):
    p = pr(relay)
    relay.expect(fixture_expect("pair_host_post", "success"))
    r = req("pair_host_post")
    _, _, _, ws, _, offer, rid = r["path"].split("/")
    assert ws == WS
    p.post_answer(offer, rid, corpus.request_body(r))


@pytest.mark.parametrize(
    "case, kind",
    [("refusal_429_rate_limited", "retry"), ("refusal_503_overloaded", "retry"), ("refusal_400_malformed", "refused"),
     ("refusal_403_not_eligible", "refused")],
)  # fmt: skip
def test_post_answer_refusals(relay, case, kind):
    p = pr(relay)
    relay.expect(fixture_expect("pair_host_post", case, body=None))
    r = req("pair_host_post")
    _, _, _, _, _, offer, rid = r["path"].split("/")
    with pytest.raises(PairRelayError) as e:
        p.post_answer(offer, rid, corpus.request_body(r))
    assert e.value.kind == kind


def test_post_answer_fenced_stops(relay):
    p = pr(relay)
    relay.expect(fixture_expect("pair_host_post", "refusal_409_fenced", body=None))
    r = req("pair_host_post")
    _, _, _, _, _, offer, rid = r["path"].split("/")
    with pytest.raises(Fenced):
        p.post_answer(offer, rid, corpus.request_body(r))


def test_post_cert_request_is_the_fixtures_body(relay):
    p = pr(relay)
    relay.expect(fixture_expect("cert_requests_post", "success"))
    r = req("cert_requests_post")
    p.post_cert_request(r["path"].split("/")[3], r["json"]["request"])


@pytest.mark.parametrize(
    "case, kind",
    [("refusal_409_request_open", "request_open"), ("refusal_409_replayed", "replayed"), ("refusal_410_expired",
     "expired"), ("refusal_429_quota_exceeded", "retry"), ("refusal_403_not_eligible", "refused"),
     ("refusal_403_other_person", "refused")],
)  # fmt: skip
def test_post_cert_request_refusals(relay, case, kind):
    p = pr(relay)
    relay.expect(fixture_expect("cert_requests_post", case, body=None))
    r = req("cert_requests_post")
    with pytest.raises(PairRelayError) as e:
        p.post_cert_request(r["path"].split("/")[3], r["json"]["request"])
    assert e.value.kind == kind


@pytest.mark.parametrize("case", ["success", "open", "signed", "rejected", "expired"])
def test_get_cert_request_states(relay, case):
    p = pr(relay)
    relay.expect(fixture_expect("cert_request_get", case))
    got = p.get_cert_request(req("cert_request_get", case)["path"].rsplit("/", 1)[1])
    assert got == corpus.case("cert_request_get", case)["response"]["json"]


def test_get_cert_request_not_found(relay):
    p = pr(relay)
    relay.expect(fixture_expect("cert_request_get", "refusal_404_not_found", body=None))
    with pytest.raises(PairRelayError) as e:
        p.get_cert_request(req("cert_request_get")["path"].rsplit("/", 1)[1])
    assert e.value.kind == "not_found"


def test_put_device_is_the_fixtures_body_and_needs_no_session(relay):
    p = pr(relay)
    relay.expect(fixture_expect("device_put", "success"))
    r = req("device_put")
    p.put_device(r["path"].rsplit("/", 1)[1], r["json"]["cert"])


@pytest.mark.parametrize(
    "case, kind",
    [("refusal_409_cert_conflict", "cert_conflict"), ("refusal_429_rate_limited", "retry"),
     ("refusal_403_revoked", "refused"), ("refusal_400_cert_invalid", "refused")],
)  # fmt: skip
def test_put_device_refusals(relay, case, kind):
    p = pr(relay)
    relay.expect(fixture_expect("device_put", case, body=None))
    r = req("device_put")
    with pytest.raises(PairRelayError) as e:
        p.put_device(r["path"].rsplit("/", 1)[1], r["json"]["cert"])
    assert e.value.kind == kind


def test_post_baseline_is_the_fixtures_body(relay):
    p = pr(relay)
    relay.expect(fixture_expect("baseline_post", "success"))
    body = req("baseline_post")["json"]
    p.post_baseline(body["hash"], body["offer_id"])


@pytest.mark.parametrize(
    "case, kind",
    [("refusal_404_not_found", "gone"), ("refusal_410_checkpoint_gone", "gone"),
     ("refusal_429_quota_exceeded", "quota"), ("refusal_403_not_eligible", "refused")],
)  # fmt: skip
def test_post_baseline_refusals(relay, case, kind):
    p = pr(relay)
    relay.expect(fixture_expect("baseline_post", case, body=None))
    body = req("baseline_post")["json"]
    with pytest.raises(PairRelayError) as e:
        p.post_baseline(body["hash"], body["offer_id"])
    assert e.value.kind == kind


def test_a_transport_failure_is_retry(relay):
    from .mock import Expect, Reply

    p = pr(relay)
    relay.expect(Expect("PUT", req("device_put")["path"], reply=Reply(fault="close")))
    r = req("device_put")
    with pytest.raises(PairRelayError) as e:
        p.put_device(r["path"].rsplit("/", 1)[1], r["json"]["cert"])
    assert e.value.kind == "retry"
