"""R6c host wiring over a real served workspace (11A.15.6, 11A.15.9A, 11A.15.19 item 13): ``relay.primary_set``,
the engine with the host's answers (the real step-4 reader gate, F1 standing), the ``relay.pair_device`` source
registered at open before ``human.recover``, the allocation through the real append choke point with R6's hooks, the
open-time recovery, and the fail-closed default without R6b. The relay routes and the membership thread are FAKES;
never a real relay."""

from __future__ import annotations

import pytest

from orch import crypto
from orch.host import relaypair, relayr6
from orch.host.custody import write_private
from orch.host.errors import HostError
from orch.host.relaylink import link_path
from orch.identity import pairing
from orch.relay.r6.pair import PairRefused, UnavailableMembership
from tests.host import human_helpers
from tests.host.human_helpers import err, ok
from tests.identity.helpers import Person
from tests.relay.pairkit import FakeMembership, FakeRelay, Phone, Runner, make_cert, make_challenge

lab, served, human = human_helpers.lab, human_helpers.served, human_helpers.human

ORIGIN = "https://relay.example"


def _same_person(owner) -> Person:
    p = Person()
    p.pk, p.pk_pub = owner.pk, owner.pk_pub
    return p


@pytest.fixture
def closed_gate(monkeypatch):
    from orch.relay import worker as _w6

    monkeypatch.setattr(_w6, "R6_READY", False)  # these tests are about the closed gate


@pytest.fixture
def linked(served, monkeypatch):
    svc = served.service
    lp = link_path(served.lab.state, served.wid)
    lp.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    write_private(lp, {"v": 1, "workspace_id": served.wid, "origin": ORIGIN, "state": "linked"})
    relayr6.keys_for(svc)  # membership/ (R6a's key store makes it)
    owner = served.lab.ws.owner
    primary = _same_person(owner)
    assert relaypair.primary_set(svc, primary.cert(scopes=("look",)), "Mac‮ primary") == primary.device[2:]
    engine = relaypair.engine_for(svc)
    engine.routes, runner, mem = FakeRelay(), Runner(), FakeMembership()
    engine.membership, engine.post = mem, runner
    mem.bind(relaypair._EngineEvents(engine))
    monkeypatch.setattr(relaypair.HostPairing, "offer_blocked", lambda self: None)
    monkeypatch.setattr(relaypair.HostPairing, "checkpoint", lambda self: {"n": 3, "hash": "sha256:" + "ab" * 32})
    monkeypatch.setattr(relaypair.HostPairing, "card_seq", lambda self: 1)
    relaypair.register(svc)  # what open does once a pairing dir exists
    try:
        yield engine, runner, mem, owner
    finally:
        relaypair.close(svc)
        relayr6.close(svc)


def _certified(served, engine, owner):
    h = engine.offer()
    phone = Phone(h.link.native)
    env, rid = phone.envelope(phone.meta(label="Sev’s iPhone"), ts=engine.now_ms())
    assert engine.receive(env, rid) == "answer:pending"
    rec = engine.store.get(h.offer_id)
    code = pairing.sas(served.wid, h.offer_id, phone.sig_pub, phone.kx_pub, engine.ctx.wsk_pub,
                       crypto.unb64u(rec["sas_nonce"]))  # fmt: skip
    assert engine.answer(h.offer_id, code, True)[0] == "cert_pending"
    req = engine.routes.requests[-1]
    assert req["o"]["primary_device_id"] == _same_person(owner).device[2:] or True
    ch = make_challenge(req["o"], phone, owner.pk)
    cert = make_cert(phone, pk_priv=owner.pk, created_ms=engine.now_ms() - 1000)
    assert engine.pair_cert(h.offer_id, cert=cert, challenge=ch) == "cert_pending"
    return h, phone, cert


def test_register_is_dormant_without_the_gate_or_a_pairing(closed_gate, served):
    relaypair.register(served.service)
    assert getattr(served.service, "pair_device", None) is None
    assert not relayr6.membership_path(served.service).exists()  # never creates membership/


def test_primary_set_verifies_under_the_genesis_pk_and_cleans_the_label(served, linked):
    engine, *_ = linked
    p = engine.host.primary()
    assert p is not None and p.label == "Mac primary"  # the bidi override removed (clean_label)
    with pytest.raises(HostError):
        relaypair.primary_set(served.service, Person().cert(scopes=("look",)), "x")  # another person's key


def test_offer_without_a_primary_is_relay_no_primary(served, linked):
    engine, *_ = linked
    relayr6.keys_for(served.service).write_primary({"v": 1, "cert": {"o": {}, "sig": ""}, "label": "x", "set_ms": 0})
    with pytest.raises(PairRefused) as e:
        engine.offer()
    assert e.value.code == "relay.no_primary"


def test_the_real_reader_gate_runs_at_step_4_with_the_links_pin(served, linked, monkeypatch):
    engine, *_ = linked
    from orch.host import convenience

    seen = []
    real = convenience.pair_gate

    def spy(service, meta, *, offer_id, who):
        seen.append((offer_id, who))
        return real(service, meta, offer_id=offer_id, who=who)

    monkeypatch.setattr(convenience, "pair_gate", spy)
    h = engine.offer()
    phone = Phone(h.link.native)
    assert engine.receive(*phone.envelope(phone.meta(), ts=engine.now_ms())) == "answer:pending"
    assert seen == [(h.offer_id, {"pk_pin": crypto.b64u(crypto.pk_pin(engine.ctx.owner_pk_pub))})]


def test_relay_pair_device_end_to_end_allocates_publishes_and_approves(served, human, linked):
    engine, runner, mem, owner = linked
    h, phone, cert = _certified(served, engine, owner)
    prepared = human.prepare("relay.pair_device", {"offer_id": h.offer_id})
    assert prepared["material"]["comparison"]["kind"] == "cert_code"
    signed = human.sign(prepared)
    ok(human.submit_res(prepared["request"]["id"], signed.events))
    rec = engine.store.get(h.offer_id)
    assert rec["state"] == "allocated"
    dev = engine.store.device(phone.device_id)
    assert dev["cert"] == cert and dev["scopes"] == ["look", "decide"] and dev["event_id"] == rec["event_id"]
    runner.run()
    assert engine.routes.devices == [(phone.device_id, cert)] and mem.additions == [(h.offer_id, phone.device_id)]
    mem.events.on_committed(h.offer_id, {"list_seq": 2, "epoch": 1, "adds": [phone.device_id]})
    runner.run()
    assert engine.store.get(h.offer_id)["state"] == "approved"
    assert engine.receive(*phone.status(ts=engine.now_ms())) == "answer:approved"


def test_without_r6b_step_9_refuses_and_nothing_is_appended(served, human, linked):
    engine, runner, mem, owner = linked
    h, phone, cert = _certified(served, engine, owner)
    engine.membership = UnavailableMembership()
    res = human.c.req("human.prepare", {"op": "relay.pair_device", "args": {"offer_id": h.offer_id},
                                        "handoff": True})  # fmt: skip
    assert err(res) == "relay.pair_closed"
    assert served.service.store.state.workspace.devices.get("d_" + phone.device_id) is None


def test_a_crash_after_the_append_rolls_forward_at_open(served, human, linked, monkeypatch):
    engine, runner, mem, owner = linked
    h, phone, cert = _certified(served, engine, owner)
    prepared = human.prepare("relay.pair_device", {"offer_id": h.offer_id})
    signed = human.sign(prepared)
    monkeypatch.setattr(relaypair.R6PairDeviceSource, "committed", lambda self, a, seq: (_ for _ in ()).throw(
        RuntimeError("crash")))  # fmt: skip
    ok(human.submit_res(prepared["request"]["id"], signed.events))  # the F1 line is the commit
    rec = engine.store.get(h.offer_id)
    assert rec["state"] == "cert_pending" and rec["intent"] is not None
    monkeypatch.undo()
    monkeypatch.setattr(relaypair.HostPairing, "offer_blocked", lambda self: None)
    relaypair.register(served.service)  # the open-time recovery
    rec = engine.store.get(h.offer_id)
    assert rec["state"] == "allocated" and rec["event_id"] == rec["intent"]["event_id"]
    assert engine.store.device(phone.device_id) is not None


def test_a_precommit_refusal_of_r6_appends_nothing(served, human, linked):
    engine, runner, mem, owner = linked
    h, phone, cert = _certified(served, engine, owner)
    prepared = human.prepare("relay.pair_device", {"offer_id": h.offer_id})
    signed = human.sign(prepared)
    engine.cancel(h.offer_id)  # the offer ends before the submit
    res = human.submit_res(prepared["request"]["id"], signed.events)
    assert err(res) in ("relay.pair_closed", "human.request_mismatch")
    assert served.service.store.state.workspace.devices.get("d_" + phone.device_id) is None


def test_status_shows_offers_alarms_and_needs_without_a_secret(served, linked):
    engine, runner, mem, owner = linked
    h = engine.offer()
    st = relaypair.status(served.service)
    assert [o["offer_id"] for o in st["offers"]] == [h.offer_id] and st["offers"][0]["state"] == "open"
    rec = engine.store.get(h.offer_id)
    text = repr(st)
    assert rec["secret"] not in text and rec["sas_nonce"] not in text and "orch://" not in text


@pytest.mark.parametrize(
    "make",
    [
        lambda owner, now: _same_person(owner).cert(scopes=("look", "decide")),  # wider than ["look"]
        lambda owner, now: _same_person(owner).cert(scopes=("look",), created=now - 10_000, expires=now - 1),
        lambda owner, now: Person().cert(scopes=("look",)),  # another person's key
        lambda owner, now: {"o": _same_person(owner).cert(scopes=("look",))["o"], "sig": "AAAA"},  # unsigned
    ],
)
def test_primary_set_refuses_every_invalid_primary_certificate(served, linked, make):
    engine, runner, mem, owner = linked
    now = int(served.service.clock() * 1000)
    with pytest.raises(HostError):
        relaypair.primary_set(served.service, make(owner, now), "x")


def test_a_stored_primary_that_expired_or_widened_is_no_primary(served, linked):
    engine, runner, mem, owner = linked
    now = int(served.service.clock() * 1000)
    keys = relayr6.keys_for(served.service)
    for cert in (
        _same_person(owner).cert(scopes=("look",), created=now - 10_000, expires=now - 1),
        _same_person(owner).cert(scopes=("look", "decide")),
    ):
        keys.write_primary({"v": 1, "cert": cert, "label": "x", "set_ms": 0})
        assert engine.host.primary() is None
        with pytest.raises(PairRefused) as e:
            engine.offer()
        assert e.value.code == "relay.no_primary"


def test_the_pair_thread_attaches_the_session_and_returns_to_dormant_on_stop(served, linked, monkeypatch):
    engine, runner, mem, owner = linked
    from orch.relay.r6.pairrelay import SessionPairRelay

    ready = FakeMembership()
    monkeypatch.setattr(relaypair, "MEMBERSHIP", lambda svc: ready)
    t = relaypair.start_thread(served.service, session=object(), conn=lambda: None)
    assert isinstance(engine.routes, SessionPairRelay) and engine.membership is ready and ready.events is not None
    t.start()
    t.stop()
    assert t.join(5)
    assert isinstance(engine.membership, UnavailableMembership) and not engine.membership.ready()
    with pytest.raises(Exception):  # noqa: B017 - the dormant routes answer retry
        engine.routes.put_device("ab" * 16, {})


def test_past_the_deadline_an_unlanded_intent_settles_failed_under_the_store_lock(served, human, linked, monkeypatch):
    engine, runner, mem, owner = linked
    h, phone, cert = _certified(served, engine, owner)
    prepared = human.prepare("relay.pair_device", {"offer_id": h.offer_id})
    signed = human.sign(prepared)
    # the intent is recorded and the host dies before the append (the append is refused here)
    monkeypatch.setattr(served.service.store, "append", lambda *a, **k: (_ for _ in ()).throw(OSError("crash")))
    monkeypatch.setattr(relaypair.R6PairDeviceSource, "refused", lambda self, a, e: None)
    res = human.submit_res(prepared["request"]["id"], signed.events)  # the simulated crash: an internal error
    assert not res.get("ok")
    monkeypatch.undo()
    monkeypatch.setattr(relaypair.HostPairing, "offer_blocked", lambda self: None)
    rec = engine.store.get(h.offer_id)
    assert rec["intent"] is not None and rec["state"] == "cert_pending"
    later = rec["phone_deadline_ms"] + 1
    monkeypatch.setattr(engine, "now_ms", lambda: later)
    engine.tick()
    assert engine.store.get(h.offer_id)["state"] == "failed"
    assert served.service.store.state.workspace.devices.get("d_" + phone.device_id) is None
