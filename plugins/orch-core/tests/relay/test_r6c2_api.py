"""R6c-2: the internal API as pair-thread jobs (11A.15.9A) and its allowlist. **Hard rule (Opus review of #588):** the
host's settlement paths (``settle``, ``committed``, ``refused``, ``recover``, ``adopt_existing``) are never reachable
through the pair thread or any op: ``PairThread`` has no generic ``call``, and ``api`` runs only the names of
``API_METHODS``. Also ``show`` and the durable ``pairing_dropped`` counter."""

from __future__ import annotations

import pytest

from orch.relay.r6 import pair as P
from orch.relay.r6.pair import PairRefused, PairThread

from .pairkit import Kit, Phone

SETTLEMENT = ("settle", "committed", "refused", "recover", "adopt_existing", "precommit", "quarantine", "facts")


@pytest.fixture
def kit(tmp_path):
    k = Kit(tmp_path)
    yield k
    k.close()


def test_the_api_is_exactly_the_9a_surface():
    assert P.API_METHODS == frozenset({"offer", "answer", "cancel", "abandon", "list_offers", "pair_cert", "show"})
    assert not P.API_METHODS & set(SETTLEMENT)


def test_the_pair_thread_has_no_generic_call():
    assert not hasattr(PairThread, "call")


@pytest.mark.parametrize("name", [*SETTLEMENT, "tick", "receive", "_commit", "publish", "nope"])
def test_api_refuses_every_name_outside_the_allowlist(kit, name):
    t = PairThread(kit.engine, name="orch-relay-r6p-test")
    with pytest.raises(PairRefused) as e:
        t.api(name)
    assert e.value.code == "invalid.input"
    with pytest.raises(PairRefused):
        P.run_api(kit.engine, name)


def test_api_runs_on_the_thread_and_show_has_no_secret(kit):
    t = PairThread(kit.engine, name="orch-relay-r6p-test")
    t.start()
    try:
        h = t.api("offer")
        phone = Phone(h.link.native)
        kit.engine.receive(*phone.envelope(phone.meta(), ts=kit.clock.ms))
        s = t.api("show", h.offer_id)
        assert (s.state, s.label, s.sas_tries_left, s.has_cert, s.path) == ("pending", "Severin's iPhone", 3, False,
                                                                             "7b")  # fmt: skip
        rec = kit.rec(h.offer_id)
        assert rec["secret"] not in repr(s) and rec["sas_nonce"] not in repr(s)
        with pytest.raises(PairRefused):
            t.api("show", "ab" * 16)
    finally:
        t.stop()
        t.join(5)


def test_pairing_dropped_is_counted_durably_at_most_once_a_minute(kit):
    kit.engine.receive(b"garbage", "ab" * 16)
    kit.engine.receive(b"garbage", "ab" * 16)
    kit.engine.tick()
    assert kit.store.alarms()["alarms"]["pairing_dropped"]["count"] == 2
    kit.engine.receive(b"garbage", "ab" * 16)
    kit.engine.tick()  # within the minute: not written again
    assert kit.store.alarms()["alarms"]["pairing_dropped"]["count"] == 2
    kit.clock.ms += 60_000
    kit.engine.tick()
    assert kit.store.alarms()["alarms"]["pairing_dropped"]["count"] == 3


def test_a_call_that_times_out_before_it_runs_is_dropped_never_applied_later(kit):
    """Opus review of #589, L2: the caller got workspace.unavailable, so the job must not run afterwards (an offer
    whose link nobody saw would block pairing)."""
    t = PairThread(kit.engine, name="orch-relay-r6p-test")  # not started yet: the job waits in the queue
    with pytest.raises(PairRefused) as e:
        t.api("offer", timeout=0.05)
    assert e.value.code == "workspace.unavailable"
    t.start()
    try:
        assert t.api("list_offers") == []  # the queued offer was dropped, never made
        assert kit.store.offer_ids() == []
    finally:
        t.stop()
        t.join(5)


def test_a_call_that_times_out_while_it_runs_is_outcome_unknown(kit):
    import threading

    t = PairThread(kit.engine, name="orch-relay-r6p-test")
    gate, entered = threading.Event(), threading.Event()
    real = kit.engine.list_offers

    def slow():
        entered.set()
        gate.wait(5)
        return real()

    kit.engine.list_offers = slow  # type: ignore[method-assign]
    t.start()
    try:
        with pytest.raises(PairRefused) as e:
            t.api("list_offers", timeout=0.2)
        assert entered.is_set() and e.value.code == "outcome.unknown"
    finally:
        gate.set()
        t.stop()
        t.join(5)
