"""``orch.relay.retry`` (11A.7 "Retries and backoff"): the refusal classes, ``Retry-After`` as a floor, full-jitter
backoff, quota handling (membership never retried unchanged; elsewhere at most 5 retries, then an alarm and an hourly
wait; a named permanent quota at once), identical bytes on every retry."""

from __future__ import annotations

import random

import pytest

from orch.net import http
from orch.relay import retry


class Clock:
    def __init__(self):
        self.t = 1000.0
        self.slept: list[float] = []

    def mono(self):
        return self.t

    def sleep(self, s):
        assert s >= 0
        self.slept.append(s)
        self.t += s


def refused(status, code, retry_after=None, quota=None):
    return retry.RelayRefused(status, code, retry_after=retry_after, quota=quota)


@pytest.mark.parametrize(
    "value,expect",
    [
        ("20", 20),
        ("0", 0),
        ("3600", 3600),
        ("7200", 3600),
        (None, None),
        ("", None),
        ("-1", None),
        ("1.5", None),
        ("20, 30", None),
        ("Wed, 21 Oct 2015 07:28:00 GMT", None),
        (" 20", None),
        ("9" * 40, None),
    ],
)
def test_retry_after_parsing(value, expect):
    assert retry.parse_retry_after(value) == expect


@pytest.mark.parametrize(
    "r,cls",
    [
        (refused(500, "internal"), retry.TRANSIENT),
        (refused(502, None), retry.TRANSIENT),
        (refused(503, "overloaded"), retry.TRANSIENT),
        (refused(429, "rate_limited"), retry.TRANSIENT),
        (refused(429, "quota_exceeded"), retry.QUOTA),
        (refused(429, "quota_exceeded", quota="nodes"), retry.PERMANENT),
        (refused(429, "quota_exceeded", quota="workspaces"), retry.PERMANENT),
        (refused(429, "quota_exceeded", quota="evidence"), retry.PERMANENT),
        (refused(413, "too_large"), retry.PERMANENT),
        (refused(400, "too_large"), retry.PERMANENT),
        (refused(400, "malformed"), retry.PERMANENT),
        (refused(409, "fenced"), retry.PERMANENT),
        (refused(401, "bad_signature"), retry.PERMANENT),
    ],
)
def test_classify(r, cls):
    assert retry.classify(r, membership=False) == cls


def test_membership_quota_is_permanent():
    assert retry.classify(refused(429, "quota_exceeded"), membership=True) == retry.PERMANENT


def test_transport_errors_classify():
    assert retry.classify(http.TransportError("deadline", "R"), membership=False) == retry.TRANSIENT
    assert retry.classify(http.RedirectRefused("R"), membership=False) == retry.PERMANENT


def test_backoff_full_jitter_bounds():
    rng = random.Random(1)
    for attempt in range(0, 20):
        for _ in range(50):
            d = retry.backoff(attempt, rng)
            assert 1.0 <= d <= 300.0
    assert max(retry.backoff(30, random.Random(i)) for i in range(200)) > 100  # reaches toward the 5 min cap


def test_retry_after_is_a_floor_capped_at_an_hour():
    rng = random.Random(0)
    assert retry.delay(0, 20, rng) >= 20
    assert retry.delay(0, 3600, rng) == 3600
    assert retry.delay(0, None, rng) <= 1.0 + 1e-9


def _retrier(clock, alarms=None, **kw):
    return retry.Retrier(
        sleep=clock.sleep,
        rng=random.Random(7),
        alarm=(lambda c, d: alarms.append((c, d))) if alarms is not None else (lambda c, d: None),
        **kw,
    )


def test_transient_then_success_resends_identical_bytes():
    clock, sent = Clock(), []
    body = b'{"a":1}'
    answers = [refused(503, "overloaded", retry_after=5), http.TransportError("deadline", "R"), "ok"]

    def fn():
        sent.append(body)
        a = answers.pop(0)
        if isinstance(a, Exception):
            raise a
        return a

    assert _retrier(clock).run(fn, route="R") == "ok"
    assert sent == [body] * 3
    assert clock.slept[0] >= 5


def test_permanent_is_raised_at_once():
    clock = Clock()

    def fn():
        raise refused(413, "too_large")

    with pytest.raises(retry.RelayRefused):
        _retrier(clock).run(fn, route="R")
    assert clock.slept == []


def test_membership_429_is_never_retried_unchanged():
    clock, calls = Clock(), []

    def fn():
        calls.append(1)
        raise refused(429, "quota_exceeded", retry_after=20)

    with pytest.raises(retry.RelayRefused):
        _retrier(clock).run(fn, route="PUT membership", membership=True)
    assert calls == [1] and clock.slept == []


def test_quota_elsewhere_five_retries_then_alarm_and_hourly_wait():
    clock, alarms, calls = Clock(), [], []

    def fn():
        calls.append(1)
        if len(calls) <= 7:
            raise refused(429, "quota_exceeded")
        return "ok"

    assert _retrier(clock, alarms).run(fn, route="R") == "ok"
    assert len(calls) == 8
    assert [a[0] for a in alarms] == ["relay_quota", "relay_quota"]
    assert clock.slept[5:] == [3600, 3600]  # after 5 backoff retries: an hour between attempts
    assert all(s < 3600 for s in clock.slept[:5])


def test_named_permanent_quota_is_not_retried():
    clock = Clock()

    def fn():
        raise refused(429, "quota_exceeded", quota="nodes")

    with pytest.raises(retry.RelayRefused):
        _retrier(clock).run(fn, route="R")
    assert clock.slept == []


def test_redirect_alarms_and_stops():
    clock, alarms = Clock(), []

    def fn():
        raise http.RedirectRefused("R")

    with pytest.raises(http.RedirectRefused):
        _retrier(clock, alarms).run(fn, route="R")
    assert alarms and alarms[0][0] == "relay_redirect" and clock.slept == []


def test_stop_ends_the_wait():
    import threading

    stop = threading.Event()
    stop.set()
    clock = Clock()

    def fn():
        raise refused(503, "overloaded")

    with pytest.raises(retry.Stopped):
        _retrier(clock, stop=stop).run(fn, route="R")


def test_max_attempts_bound():
    clock = Clock()

    def fn():
        raise refused(503, "overloaded")

    with pytest.raises(retry.RelayRefused):
        _retrier(clock).run(fn, route="R", max_attempts=3)
    assert len(clock.slept) == 2


def test_refusal_from_response():
    from orch.net.http import Response

    r = retry.RelayRefused.from_response(
        "R",
        Response(
            429,
            {"retry-after": "20", "content-type": "application/json"},
            b'{"error":"quota_exceeded","quota":"nodes"}',
        ),
    )
    assert (r.status, r.code, r.retry_after, r.quota) == (429, "quota_exceeded", 20, "nodes")
    junk = retry.RelayRefused.from_response("R", Response(502, {}, b"<html>bad gateway SECRET</html>"))
    assert (junk.status, junk.code) == (502, None)
    assert "SECRET" not in str(junk) and "SECRET" not in repr(junk)
    weird = retry.RelayRefused.from_response("R", Response(400, {}, b'{"error":"Bad\\r\\nCode"}'))
    assert weird.code is None


# --- waits honour the stop event (review L2, 11A.7: stop joins within 5 s) -------------------------------------------


def _stop_soon():
    import threading

    stop = threading.Event()
    threading.Timer(0.2, stop.set).start()
    return stop


@pytest.mark.parametrize(
    "err",
    [
        refused(429, "rate_limited", retry_after=3600),  # a long Retry-After
        refused(503, "overloaded"),  # backoff
    ],
)
def test_a_long_wait_ends_when_stop_is_set(err):
    import time

    stop = _stop_soon()
    r = retry.Retrier(sleep=lambda s: time.sleep(s), alarm=lambda c, d: None, rng=random.Random(1), stop=stop)
    calls = []

    def fn():
        calls.append(1)
        if len(calls) > 1:
            raise AssertionError("retried after stop")
        raise err

    t0 = time.monotonic()
    with pytest.raises(retry.Stopped):
        r.run(fn, route="R")
    assert time.monotonic() - t0 < 5.0


def test_the_hourly_quota_wait_ends_when_stop_is_set():
    import time

    stop = _stop_soon()
    r = retry.Retrier(sleep=lambda s: time.sleep(s), alarm=lambda c, d: None, rng=random.Random(1), stop=stop)

    def fn():
        raise refused(429, "quota_exceeded")

    t0 = time.monotonic()
    with pytest.raises(retry.Stopped):
        r.run(fn, route="R")
    assert time.monotonic() - t0 < 5.0
