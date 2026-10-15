"""R3 against a hostile or failing relay (11A.9 threat model, 11A.10 R3 release gate rows): lies about the
acknowledgement and the staging state, replays, false ``chain_diverged``, 5xx storms, a slow relay, quota and rate
answers with ``Retry-After``, conflicts, clock refusals, a fence. The outbox entry is never deleted on any of them."""

from __future__ import annotations

import pytest

from orch.relay import publish
from orch.relay.session import Fenced

from .mock import Reply, json_reply
from .pubkit import Kit
from .sim import _err


@pytest.fixture
def kit(tmp_path):
    k = Kit(tmp_path, tickets=2, events=1)
    yield k
    k.close()


def _is(s, kind):
    if kind == "final":
        return s.method == "PUT" and s.path.endswith("/checkpoint")
    if kind == "page":
        return s.method == "PUT" and "/pages/" in s.path
    if kind == "manifest":
        return s.method == "PUT" and s.path.endswith("/manifest")
    raise AssertionError(kind)


def _first_n(kind, n, reply):
    count = {"n": 0}

    def fault(s):
        if _is(s, kind) and count["n"] < n:
            count["n"] += 1
            return reply() if callable(reply) else reply
        return None

    return fault


def _untouched(k, before):
    assert [e.n for e in k.store.outbox_entries()] == before


def test_a_5xx_storm_backs_off_with_growing_waits_and_then_publishes(kit):
    k = kit
    k.sim.fault = _first_n("page", 6, lambda: _err(503, "overloaded"))
    waits = []
    for _ in range(40):
        state, wait = k.publisher.step()
        if state == "wait":
            waits.append(wait)
        if state == "idle":
            break
    assert len(waits) == 6 and all(w >= 1.0 for w in waits) and max(waits) > min(waits)
    assert k.store.outbox_entries() == [] and not k.alarms


def test_rate_limited_honours_retry_after_as_a_floor(kit):
    k = kit
    k.sim.fault = _first_n("manifest", 1, lambda: _err(429, "rate_limited", retry_after=37))
    state, wait = k.publisher.step()
    assert state == "wait" and wait >= 37 and not k.alarms


def test_quota_is_never_retried_unchanged_and_alarms_after_five(kit):
    k = kit
    before = [e.n for e in k.store.outbox_entries()]
    k.sim.fault = lambda s: _err(429, "quota_exceeded", retry_after=9) if _is(s, "page") else None
    waits = [k.publisher.step() for _ in range(7)]
    assert all(state == "wait" for state, _ in waits)
    assert all(w >= 9 for _, w in waits[:5])
    assert waits[5][1] == publish.QUOTA_WAIT_S and waits[6][1] == publish.QUOTA_WAIT_S
    assert k.alarms.codes() == ["relay_quota"]
    _untouched(k, before)


def test_a_permanent_quota_alarms_at_once(kit):
    k = kit
    k.sim.fault = lambda s: (json_reply(429, {"error": "quota_exceeded", "quota": "nodes"}, {"Retry-After": "1"})
                             if _is(s, "final") else None)  # fmt: skip
    state, wait = k.publisher.step()
    assert (state, wait) == ("wait", publish.HELD_RECHECK_S) and k.alarms.codes() == ["relay_quota"]


def test_a_manifest_that_lies_complete_never_deletes_and_ends_misbehaving(kit):
    k = kit
    before = [e.n for e in k.store.outbox_entries()]
    k.sim.fault = lambda s: json_reply(200, {"complete": True, "missing": []}) if _is(s, "manifest") else None
    for _ in range(8):
        k.publisher.step()
        if "relay_misbehaving" in k.alarms.codes():
            break
    assert "relay_misbehaving" in k.alarms.codes() and k.sim.accepted == []
    _untouched(k, before)


def test_a_manifest_answer_naming_pages_that_do_not_exist_is_misbehaving(kit):
    k = kit
    k.sim.fault = lambda s: json_reply(200, {"complete": False, "missing": [99]}) if _is(s, "manifest") else None
    assert k.publisher.step()[0] == "wait" and k.alarms.codes() == ["relay_misbehaving"]


def test_a_manifest_that_keeps_reporting_pages_missing_is_misbehaving(kit):
    k = kit
    k.sim.fault = lambda s: json_reply(200, {"complete": False, "missing": [0]}) if _is(s, "manifest") else None
    assert k.publisher.step()[0] == "wait" and k.alarms.codes() == ["relay_misbehaving"]


def test_a_replayed_old_acknowledgement_deletes_nothing(kit):
    k = kit
    k.drain()
    old_n, old_h = k.sim.accepted[-1]
    k.env.log(k.uids[0], "new")
    before = [e.n for e in k.store.outbox_entries()]
    k.sim.after = lambda s, r: (
        json_reply(200, {"result": "accepted", "n": old_n, "hash": old_h}) if _is(s, "final") else r
    )
    assert k.publisher.step()[0] == "wait" and k.alarms.codes() == ["relay_misbehaving"]
    _untouched(k, before)


def test_ignored_for_a_staged_extension_is_a_lie(kit):
    k = kit

    def ignored(s, r):
        if _is(s, "final"):
            k.sim.held = None  # it kept nothing
            import json

            body = json.loads(r.body)
            return json_reply(200, {**body, "result": "ignored"})
        return r

    k.sim.after = ignored
    assert k.publisher.step()[0] == "wait" and k.alarms.codes() == ["relay_misbehaving"]
    assert k.publisher.history.lines() == []


def test_a_false_chain_diverged_is_never_evidence(kit):
    k = kit
    before = [e.n for e in k.store.outbox_entries()]
    k.sim.fault = lambda s: _err(409, "chain_diverged") if _is(s, "final") else None
    assert k.publisher.step() == ("progress", 0.0)  # re-read H and classify: contained, plan again
    assert k.publisher.step()[0] == "wait"
    assert k.alarms.codes() == ["relay_misbehaving"] and k.diverged == []
    _untouched(k, before)
    k.sim.fault = None
    k.drain()
    assert k.store.outbox_entries() == []


def test_a_slow_relay_ends_at_the_deadline_and_the_set_resumes(kit, monkeypatch):
    k = kit
    monkeypatch.setattr(publish, "STAGE_DEADLINE_S", 1.0)
    k.sim.fault = _first_n("page", 1, lambda: Reply(status=200, body=b"{}", fault="drip_headers", drip_s=0.2))
    state, wait = k.publisher.step()
    assert state == "wait" and wait >= 1 and not k.alarms
    k.drain()
    assert k.store.outbox_entries() == []


def test_staging_conflict_drops_the_set_and_waits_for_its_expiry(kit):
    k = kit
    k.sim.fault = _first_n("page", 1, lambda: _err(409, "staging_conflict"))
    assert k.publisher.step() == ("wait", publish.STAGING_EXPIRY_S)
    assert k.alarms.codes() == ["relay_staging_conflict"] and k.state.read("publish.json") is None


def test_checkpoint_time_backs_off_and_alarms_clock_skew_after_three(kit):
    k = kit
    k.sim.fault = lambda s: _err(409, "checkpoint_time") if _is(s, "final") else None
    for _ in range(3):
        assert k.publisher.step()[0] == "wait"
    assert k.alarms.codes() == ["clock_skew"]
    finals = [s for s in k.mock.seen if _is(s, "final")]
    assert len({s.body for s in finals}) == 1  # never re-signed: the same bytes every time


def test_a_redirect_is_a_permanent_alarm(kit):
    k = kit
    k.sim.fault = lambda s: (
        Reply(status=307, headers={"Location": "https://elsewhere"}, body=b"") if _is(s, "page") else None
    )
    assert k.publisher.step() == ("wait", publish.HELD_RECHECK_S) and k.alarms.codes() == ["relay_redirect"]


def test_a_fence_mid_publication_propagates_and_deletes_nothing(kit):
    k = kit
    before = [e.n for e in k.store.outbox_entries()]
    k.sim.fault = _first_n("manifest", 1, lambda: _err(409, "fenced"))
    with pytest.raises(Fenced):
        k.publisher.step()
    _untouched(k, before)


def test_a_checkpoint_the_relay_serves_that_does_not_verify_is_misbehaving(kit):
    k = kit
    k.drain()
    held = k.sim.held
    k.sim.held = {"o": held["o"], "sig": held["sig"][:-4] + "AAAA"}
    assert k.publisher.step()[0] == "wait" and k.alarms.codes() == ["relay_misbehaving"]
    assert k.diverged == []
