"""N4 (c): a webhook ring wakes a follow-up in a workspace joined to a hub in Full mode (host-actor events only)."""

import json  # noqa: F401

from orch.model import followup_events as fe
from tests.host.test_agents import w  # noqa: F401
from tests.host.test_followups_runner import env  # noqa: F401
from tests.model.test_hubledger import HUB, ROOT


def join_full(e, monkeypatch):
    from orch.hubclient import wire

    monkeypatch.setattr(wire, "check_recorded_owner_proof", lambda *a, **k: None)
    e.w.person_event("hub.joined", hub_id=HUB, hk_root_pub=ROOT, mode="full", binding_seq=1, aliases=[], owner_proof=None)


def test_webhook_ring_fires_in_full_mode(env, monkeypatch):  # noqa: F811
    e = env
    join_full(e, monkeypatch)
    hid = fe.new_hook_id()
    on = fe.parse_on("webhook:" + hid, e.w.wid)
    uid, _, fid, _ = e.schedule_on(on, "webhook", hook=on["hook"])
    e.tick()
    assert e.w.h.run(e.runner.ring(on["hook"])) is True
    e.tick()
    rec = e.view(uid)
    assert rec["state"] == "fired", (rec, e.runner.status())


def test_time_followup_fires_in_full_mode(env, monkeypatch):  # noqa: F811
    e = env
    join_full(e, monkeypatch)
    uid, fid, due = e.schedule()
    e.w.lab.ws.clock[0] = due
    e.tick()
    assert e.view(uid)["state"] == "fired", e.runner.status()


def test_a_ring_before_the_next_tick_is_not_lost(env, monkeypatch):  # noqa: F811
    """The real cause of run-12 6.6: the host learned the pending hooks only on a tick (30 s on a real host) and a webhook
    sender rings once. The hook is now known when the follow-up is set; unknown hashes cost no store read."""
    e = env
    join_full(e, monkeypatch)
    e.w.service.followups = e.runner  # what the host does for a live workspace
    e.tick()  # a tick that knows no webhook follow-up yet
    hid = fe.new_hook_id()
    on = fe.parse_on("webhook:" + hid, e.w.wid)
    uid, _, fid, _ = e.schedule_on(on, "webhook", hook=on["hook"])  # set after the tick, then no tick
    calls = []
    real = e.w.service.call

    async def counting(*a):
        calls.append(a)
        return await real(*a)

    e.w.service.call = counting
    for _ in range(100):
        assert e.w.h.run(e.runner.ring(fe.hook_hash(fe.new_hook_id(), e.w.wid))) is False
    assert not calls, "an unknown hash must cost no store read"
    e.w.service.call = real
    e.mono[0] += 10
    assert e.w.h.run(e.runner.ring(on["hook"])) is True  # the flood did not starve the real hook
    e.tick()
    assert e.view(uid)["state"] == "fired" and e.view(uid)["trigger"] == "webhook"
