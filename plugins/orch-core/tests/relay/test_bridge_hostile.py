"""R2 against a hostile relay and a hostile member (11A.9, 11A.10): reorder, drop, replay, a forged device id, a stale
epoch; authorization before any state change or dispatch; every attacker-controlled count and size bounded."""

from __future__ import annotations

import secrets

import pytest

from orch.relay import bridge
from orch.relay.bridge import Item
from orch.wsproto import wire

from .bridgekit import Phone, World, open_chunk


@pytest.fixture
def w(tmp_path):
    world = World(tmp_path)
    yield world
    world.close()


def _meta(w, chunks):
    return open_chunk(chunks[0], w.wk, w.key.public_key)[1]


def test_reordered_requests_both_run_inside_the_window(w):
    late = w.phone.request(w.wk, {"op": "whoami", "args": {}}, seq=2)
    early = w.phone.request(w.wk, {"op": "needs", "args": {}}, seq=1)
    assert _meta(w, w.send(late)) == {"status": "ok"}
    assert _meta(w, w.send(early)) == {"status": "ok"}
    assert w.host.reads == ["whoami", "needs"]


def test_a_dropped_answer_is_recovered_by_the_devices_identical_resend(w):
    w.post.script = {0: "retry"}  # the relay lost it
    env = w.phone.request(w.wk, w.new_ticket())
    w.send(env)
    w.clock.ms += 20_000
    assert len(set(w.send(env))) == 1 and len(w.host.log) == 1


def test_a_relay_replaying_one_request_forever_gets_at_most_8_reposts_10_s_apart(w):
    env = w.phone.request(w.wk, {"op": "whoami", "args": {}})
    w.send(env)
    for _ in range(50):
        w.send(env)
        w.clock.ms += 1_000
    assert len(w.post.for_rid(env[40:56])) == 1 + 5  # 50 s of replays: one re-post per 10 s
    for _ in range(200):
        w.send(env)
        w.clock.ms += 10_000
    assert len(w.post.for_rid(env[40:56])) == 1 + 8  # capped at the relay's 8 re-stores
    assert w.host.reads == ["whoami"]


def test_a_request_naming_another_device_but_signed_by_this_one_is_bad_signature(w):
    victim = Phone()
    w.r6.add(victim)
    env = w.phone.request(w.wk, {"op": "whoami", "args": {}})
    forged = env[:24] + bytes.fromhex(victim.device) + env[40:]  # the header is the AAD: the tag fails first
    assert w.send(forged) == []
    # sealed under the right key with the victim's id, signed by the attacker
    env2 = victim.request(w.wk, {"op": "whoami", "args": {}}, signer=w.phone.dk)
    assert _meta(w, w.send(env2)) == {"refusal": "bad_signature"}
    assert w.host.reads == [] and w.rids.window(victim.device) == (0, 0)


@pytest.mark.parametrize("code", ["revoked", "not_member", "bad_signature"])
def test_step_4_refusals_change_no_window_and_dispatch_nothing(w, code):
    p = Phone()
    if code != "not_member":
        w.r6.add(p)
    if code == "revoked":
        w.host.standings[("d_" + p.device, "p_" + p.person)] = "revoked"
    env = p.request(w.wk, w.new_ticket(), signer=w.phone.dk if code == "bad_signature" else None)
    assert _meta(w, w.send(env)) == {"refusal": code}
    assert w.rids.window(p.device) == (0, 0) and w.host.runs == [] and w.host.reads == []
    assert w.rids.dump()["submission"] == []


def test_nothing_is_dispatched_before_the_scope_check(tmp_path):
    w = World(tmp_path)
    try:
        look = Phone(("look",))
        w.r6.add(look)
        w.phone = look
        assert _meta(w, w.send(look.request(w.wk, w.new_ticket()))) == {"refusal": "forbidden_scope"}
        assert w.host.runs == [] and w.rids.dump()["submission"] == []
    finally:
        w.close()


def test_a_replay_of_a_known_rid_is_only_served_to_a_device_that_still_holds(w):
    env = w.phone.request(w.wk, {"op": "whoami", "args": {}})
    w.send(env)
    del w.r6.members[w.phone.device]
    w.clock.ms += 20_000
    assert w.send(env) == []  # step 4 fails: the stored answer is not re-posted


def test_unverified_records_are_bounded_by_the_host_wide_budget(w):
    for _ in range(40):
        w.send(Phone().request(w.wk, {"op": "whoami", "args": {}}))
    assert len(w.rids.dump()["rid"]) == bridge.BUDGET


def test_the_budget_is_not_defeated_by_varying_device_or_rid(w):
    stranger = Phone()
    for _ in range(30):
        w.send(stranger.request(w.wk, {"op": "whoami", "args": {}}, rid=secrets.token_bytes(16)))
    assert len(w.post.posts) == bridge.BUDGET


def test_a_request_over_1_mib_is_dropped_before_any_work(w):
    env = w.phone.request(w.wk, b"\x00" * (wire.MAX_REQUEST - wire.OVERHEAD + 1))
    assert len(env) == wire.MAX_REQUEST + 1
    assert w.send(env) == [] and w.rids.dump()["rid"] == []


def test_meta_over_64_kib_is_malformed_and_uses_no_seq(w):
    big = {"op": "whoami", "args": {"x": "y" * (wire.MAX_META + 1)}}
    from orch import canon

    m = canon.dumps(big)
    pt = len(m).to_bytes(4, "big") + m
    assert _meta(w, w.send(w.phone.request(w.wk, pt, seq=9))) == {"refusal": "malformed"}
    assert w.rids.window(w.phone.device) == (0, 0)


def test_items_are_shape_checked():
    from orch.relay.bridge import _items

    good = {"n": 1, "route": "bridge", "device": "aa" * 16, "rid": "bb" * 16, "offer": None, "envelope": "AAAA"}
    bad = [
        {**good, "n": 0}, {**good, "n": True}, {**good, "route": "x"}, {**good, "device": "a"},
        {**good, "extra": 1}, {**good, "envelope": "!!"}, "x",
    ]  # fmt: skip
    assert [i.n for i in _items({"items": [good, *bad]})] == [1]
    assert _items({"items": "nope"}) == []
    assert isinstance(_items({"items": [good]})[0], Item)


def test_held_evidence_arriving_between_receipt_and_run_refuses_inside_the_job(w):
    env = w.phone.request(w.wk, w.new_ticket())
    adm = w.bridge.receive(w.item(env))
    w.host.standings[("d_" + w.phone.device, "p_" + w.phone.person)] = "revoked"
    w.bridge.run(adm)
    assert _meta(w, w.post.for_rid(env[40:56]))["refusal"] == "op.not_allowed"
    assert w.host.runs == [] and w.host.log == {}
