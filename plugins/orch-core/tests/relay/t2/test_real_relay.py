"""T2 (11A.10 harness layer 2): core's R1 client and R2 bridge loop against the REAL orch-relay of ``SOURCE``.

Every test gets its own relay process (fresh data directory, fresh rate windows), started twice over through the
``relay`` fixture's two parametrisations. What R6 will do (member list, WK grant, the device) is ref-built here (see
``kit.py``); the host side is core's own code: :class:`Session`, :class:`Directory`, :class:`Bridge`,
:class:`BridgeLoop`."""

from __future__ import annotations

import time

import pytest

from orch.relay import http as rhttp
from orch.relay.framing import MAX_ANSWER
from orch.relay.retry import RelayRefused
from orch.relay.session import Fenced, LoginHeldOff

from ..helpers import OPEN_CONNS, WS, directory, make_card
from .harness import get_json
from .kit import DEVICE, HostSide, Members, Owner, RefDevice, answer_of, publish_card, session, wait_for

pytestmark = pytest.mark.relay_t2


def linked(relay):
    """Account (operator invite), phone registered, first card PUT; returns the owner."""
    owner = Owner(relay)
    owner.create_account()
    owner.register("phone")
    sess = session(relay)
    assert publish_card(sess, relay) == 1
    return owner


# -- the relay itself ----------------------------------------------------------------------------------------------


def test_relay_runs_with_our_origin_instance_and_publication_cap(relay):
    status, health = get_json(relay.port, "/healthz")
    assert status == 200 and health["instance"] == relay.instance
    conn = rhttp.RelayConn(relay.origin)  # core's own transport, plain http only for a loopback origin
    OPEN_CONNS.append(conn)
    r = conn.request("GET", "/v2/relay-info", route="GET /v2/relay-info", headers={}, body=None, deadline_s=10,
                     cap=4096)  # fmt: skip
    assert r.status == 200
    import json

    info = json.loads(r.body)
    assert info["max_publication_bytes"] == relay.configured_cap
    assert info["reader"] >= 1


# -- R1 against the real relay -------------------------------------------------------------------------------------


def test_owner_account_card_and_verified_reads(relay):
    owner = linked(relay)
    # a used invite is single use: the same account cannot be created again
    status, obj = owner.http.call("POST", "/v2/accounts", {"invite": "x", "pk_pub": "x", "device_cert": {},
                                                           "challenge": "x", "sig": "x"})  # fmt: skip
    assert status >= 400
    sess = session(relay)
    d = directory(sess)
    # the relay holds a card now: a session-less PUT is 401 and put_card(first=True) says so with None
    assert d.put_card(make_card(relay.origin), first=True) is None
    sess.login()
    card = d.get_card()  # verified under the owner's person key, this workspace, this WSK
    assert card is not None and card["o"]["relay_url"] == relay.origin
    assert d.get_members() is None  # nothing published yet


def test_login_with_the_exact_origin_and_bad_signature_with_another(relay):
    linked(relay)
    good = session(relay)
    good.login()
    assert good.logged_in
    other = f"http://127.0.0.1:{relay.port + 1 if relay.port < 65535 else relay.port - 1}"
    bad = session(relay, origin=other)
    with pytest.raises(RelayRefused) as e:
        bad.login()
    assert (e.value.status, e.value.code) == (401, "bad_signature")
    with pytest.raises(LoginHeldOff):  # no retry within 60 s of a bad_signature
        bad.login()
    for host in ("localhost",):  # the same port under another spelling is another origin
        with pytest.raises(RelayRefused) as e:
            session(relay, origin=f"http://{host}:{relay.port}").login()
        assert e.value.code == "bad_signature"


def test_hold_then_a_takeover_fences_the_first_session(relay):
    linked(relay)
    first = session(relay)
    h1 = first.hold()
    assert h1.gen == 1 and h1.lease_s == 40
    assert first.hold().gen == 1  # a renewal keeps the generation
    second = session(relay)
    h2 = second.operator_takeover()
    assert h2.gen == 2
    with pytest.raises(Fenced):
        first.hold()
    assert first.fenced
    with pytest.raises(Fenced):  # every later write is refused locally, nothing sent
        directory(first).put_card(make_card(relay.origin, card_seq=2), first=False)
    # the new holder writes under W(g): the next card goes through
    assert directory(second).put_card(make_card(relay.origin, card_seq=2), first=False) == 2


# -- R2: the bridge loop against the real relay --------------------------------------------------------------------


@pytest.fixture
def bridged(relay, R, tmp_path):
    """Linked, the host holding, the ref-built member list and WK grant at the host's committed epoch published, the
    phone logged in with its WK; the loop NOT started."""
    linked(relay)
    sess = session(relay)
    sess.hold()
    side = HostSide(relay, tmp_path, sess)
    epoch = side.epochs.current_epoch()
    Members(R).publish(sess, epoch, side.epochs.keys[epoch])
    phone = RefDevice(relay, R)
    phone.login()
    assert phone.fetch_grants() == {epoch: side.epochs.keys[epoch]}  # the device holds exactly the host's WK
    try:
        yield side, phone, epoch
    finally:
        side.close()


def test_a_ref_built_read_is_polled_answered_and_opened_by_the_device(bridged):
    side, phone, epoch = bridged
    side.start()
    rid, env = phone.request({"op": "whoami", "args": {}}, epoch=epoch)
    assert phone.post(rid, env)[0] == 202
    results, _ = phone.answer(rid, epoch)
    meta, data = answer_of(results)
    assert meta == {"status": "ok"} and not results[0]["refusal"]
    assert data["data"]["op"] == "whoami" and data["data"]["principal"] == "r:d_" + DEVICE
    assert side.host.reads == ["whoami"]
    assert side.loop.error is None and not side.alarms


def test_a_ref_built_write_lands_once(bridged):
    side, phone, epoch = bridged
    side.start()
    log = _ulid()
    ev = side.phone.event(log, "ticket.created", eid=_ulid(), key="DEMO-1234", ticket_type="bug", title="T2",
                          owner="p_" + side.phone.person)  # fmt: skip
    rid, env = phone.request({"op": "new_ticket", "args": {"log": log, "events": [ev]}, "idem": ev["id"]}, epoch=epoch)
    assert phone.post(rid, env)[0] == 202
    meta, data = answer_of(phone.answer(rid, epoch)[0])
    assert meta == {"status": "ok"}, meta
    assert len(side.host.log) == 1 and side.host.runs == [("new_ticket", ev["id"])]


def test_a_requeued_rid_is_answered_with_the_same_bytes_and_never_run_twice(bridged):
    side, phone, epoch = bridged
    side.start()
    rid, env = phone.request({"op": "whoami", "args": {}}, epoch=epoch)
    assert phone.post(rid, env)[0] == 202
    _, first = phone.answer(rid, epoch)
    assert wait_for(lambda: len(side.loop.for_rid(rid)) == 1)
    time.sleep(10.3)  # the relay requeues an identical device resend at most once per 10 s (P3-D27)
    status, _ = phone.post(rid, env)
    assert status == 202
    assert wait_for(lambda: len(side.loop.for_rid(rid)) == 2, timeout=10)
    a, b = side.loop.for_rid(rid)
    assert a == b == first[0]  # the stored answer, byte for byte
    assert side.host.reads == ["whoami"]  # the op ran once
    assert side.loop.error is None


def test_a_request_under_an_old_epoch_gets_a_signed_stale_epoch(bridged):
    side, phone, epoch = bridged
    side.epochs.rotate()  # the host committed epoch + 1; the relay's list and the device still say `epoch`
    side.start()
    rid, env = phone.request({"op": "whoami", "args": {}}, epoch=epoch)
    assert phone.post(rid, env)[0] == 202
    results, _ = phone.answer(rid, epoch)  # device_check: WSK signature, the old epoch's key, the mailbox
    (res,) = results
    assert res["refusal"] and res["last"]
    assert res["meta"] == {"refusal": "stale_epoch", "epoch": epoch + 1}
    assert res.get("fetch_grants") is True
    assert side.host.reads == []


def test_an_answer_over_the_relay_budget_is_refused_up_front(bridged):
    side, phone, epoch = bridged
    side.start()
    rid, env = phone.request({"op": "ticket.text", "args": {"size": MAX_ANSWER + 1}}, epoch=epoch)
    assert phone.post(rid, env)[0] == 202
    results, _ = phone.answer(rid, epoch)
    (res,) = results
    assert res["refusal"] and res["meta"]["refusal"] == "frame.too_large", res["meta"]
    assert len(side.loop.for_rid(rid)) == 1  # one refusal chunk; no data chunk ever left
    # just under the budget is delivered in full
    rid2, env2 = phone.request({"op": "ticket.text", "args": {"size": 1 << 20}}, epoch=epoch)
    assert phone.post(rid2, env2)[0] == 202
    meta, data = answer_of(phone.answer(rid2, epoch, timeout_s=19)[0])
    assert meta == {"status": "ok"} and len(data["data"]["text"]) == 1 << 20


def test_requests_reordered_before_the_host_are_each_answered_once(bridged):
    side, phone, epoch = bridged
    side.start()
    late_rid, late = phone.request({"op": "needs", "args": {}}, epoch=epoch, seq=5)
    early_rid, early = phone.request({"op": "whoami", "args": {}}, epoch=epoch, seq=3)
    assert phone.post(late_rid, late)[0] == 202  # seq 5 reaches the relay (and the host) before seq 3
    assert phone.post(early_rid, early)[0] == 202
    m5, _ = answer_of(phone.answer(late_rid, epoch)[0])
    m3, _ = answer_of(phone.answer(early_rid, epoch)[0])
    assert m5 == m3 == {"status": "ok"}
    assert sorted(side.host.reads) == ["needs", "whoami"]
    # the same seq again under a NEW rid: below the window's high water and already used, a signed refusal, no run
    rid, env = phone.request({"op": "whoami", "args": {}}, epoch=epoch, seq=3)
    assert phone.post(rid, env)[0] == 202
    (res,) = phone.answer(rid, epoch)[0]
    assert res["refusal"] and res["meta"]["refusal"] == "stale_sequence", res["meta"]
    assert sorted(side.host.reads) == ["needs", "whoami"]


def test_a_request_withheld_until_the_host_started_is_fenced_not_run(bridged):
    """Stamped before this worker started and unknown to it (the relay held it while the host was away): the start
    fence answers ``already_done {status: unknown}`` and never runs it; the device re-issues under a new rid."""
    side, phone, epoch = bridged
    rid, env = phone.request({"op": "whoami", "args": {}}, epoch=epoch)
    assert phone.post(rid, env)[0] == 202
    time.sleep(0.01)
    side.start()
    (res,) = phone.answer(rid, epoch)[0]
    assert res["refusal"] and res["meta"] == {"refusal": "already_done", "status": "unknown"}
    assert side.host.reads == []
    rid2, env2 = phone.request({"op": "whoami", "args": {}}, epoch=epoch)
    assert phone.post(rid2, env2)[0] == 202
    assert answer_of(phone.answer(rid2, epoch)[0])[0] == {"status": "ok"}
    assert side.host.reads == ["whoami"]


def test_a_takeover_fences_the_running_loop(bridged, relay):
    side, phone, epoch = bridged
    side.start()
    rid, env = phone.request({"op": "whoami", "args": {}}, epoch=epoch)
    assert phone.post(rid, env)[0] == 202
    phone.answer(rid, epoch)
    session(relay).operator_takeover()
    assert wait_for(lambda: side.loop.error is not None, timeout=15)
    assert isinstance(side.loop.error, Fenced)
    assert side.loop.join(10)


def _ulid() -> str:
    from .kit import now_ms
    from ..bridgekit import ulid

    return ulid(now_ms())

