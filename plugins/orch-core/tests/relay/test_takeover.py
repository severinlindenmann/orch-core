"""The restart takeover decided by orch-52 (13 Oct 2026), core-only and fail closed, against the simulated relay
(whose ``host/hold`` is orch-relay's: a new generation is the row's ``gen + 1``; ``take_over`` steals a live lease)."""

from __future__ import annotations

import pytest

from orch import canon
from orch.relay.session import Fenced, RelayMisbehaving, TakeoverRefused
from orch.relay.state import FileHolderStore, HolderRecord, StateDir, regenerate_instance_id

from .helpers import OWNER_PK, WS, Alarms, Clock, Holder, make_card
from .helpers import session as make_session
from .mock import Expect, MockRelay, json_reply
from .sim import SimRelay

IID = "0" * 32


@pytest.fixture
def env():
    m = MockRelay()
    sim = SimRelay(m.origin(), owner_pk=OWNER_PK)
    sim.cards[WS] = make_card(m.origin())
    m.fallback = sim
    yield m, sim
    assert m.errors == []
    m.close()


def new(m, sim, holder, clock=None, alarms=None):
    clock = clock or Clock(sim)
    alarms = alarms if alarms is not None else Alarms()
    return make_session(m.origin(), holder=holder, clock=clock, alarms=alarms), clock, alarms


def holds(m):
    return [canon.loads_strict(x.body) for x in m.seen if x.path.endswith("/host/hold")]


def test_renew_with_the_stored_gen_while_the_lease_is_live(env):
    m, sim = env
    sim.holders[WS] = (3, sim.now + 40_000)
    h = Holder(gen=3)
    s, _, alarms = new(m, sim, h)
    assert s.hold().gen == 3
    assert holds(m) == [{"gen": 3, "take_over": False}] and alarms == []


def test_g_plus_1_is_accepted_and_persisted_before_anything_else(env):
    m, sim = env
    sim.holders[WS] = (3, sim.now - 1)  # our lease lapsed
    h = Holder(gen=3)
    s, _, alarms = new(m, sim, h)
    assert s.hold().gen == 4
    assert holds(m) == [{"gen": 3, "take_over": False}, {"gen": None, "take_over": False}]
    assert [r.pending for r in h.records] == [4, None]  # pending written before the probe, cleared with the new gen
    assert h.rec == HolderRecord(True, 4, IID, None) and alarms == []


def test_g_plus_2_alarms_and_writes_nothing_under_the_new_gen(env):
    m, sim = env
    sim.holders[WS] = (4, sim.now - 1)  # someone else held 4 meanwhile
    h = Holder(gen=3)
    s, _, alarms = new(m, sim, h)
    with pytest.raises(TakeoverRefused):
        s.hold()
    assert alarms.codes() == ["relay_takeover_refused"]
    assert "relay gen 5 != stored+1 (4)" in alarms[0][1]
    assert h.rec.gen == 3 and not h.rec.last_holder and h.rec.pending is None
    n = len(m.seen)
    with pytest.raises(Fenced):
        s.call("PUT", f"/v2/workspaces/{WS}/card", "R", body={}, holder=True)
    with pytest.raises(Fenced):
        s.hold()
    assert len(m.seen) == n


def test_holder_conflict_waits_and_never_steals(env):
    m, sim = env
    sim.holders[WS] = (7, sim.now + 40_000)  # a rival's live lease; our stored gen 3 is fenced
    h = Holder(gen=3)
    s, clock, alarms = new(m, sim, h)
    with pytest.raises(TakeoverRefused):
        s.hold()
    bodies = holds(m)
    assert {"gen": None, "take_over": True} not in bodies
    assert bodies[:3] == [
        {"gen": 3, "take_over": False},
        {"gen": None, "take_over": False},
        {"gen": None, "take_over": False},
    ]
    assert 40.0 in clock.slept and "relay gen 8 != stored+1 (4)" in alarms[0][1]


def test_pending_record_alarms_and_does_not_probe(env):
    m, sim = env
    h = Holder(gen=3, pending=4)
    s, _, alarms = new(m, sim, h)
    with pytest.raises(TakeoverRefused):
        s.hold()
    assert alarms.codes() == ["relay_takeover_refused"]
    assert "possibly our own interrupted takeover" in alarms[0][1] and "orch host relay takeover" in alarms[0][1]
    assert holds(m) == []


def test_not_the_last_holder_alarms_without_probing(env):
    m, sim = env
    sim.holders[WS] = (3, sim.now - 1)
    s, _, alarms = new(m, sim, Holder(gen=3, last_holder=False))
    with pytest.raises(TakeoverRefused):
        s.hold()
    assert "(a)" in alarms[0][1] and holds(m) == []


def test_other_instance_id_renews_but_never_probes(env):
    """orch-52's order: the renew with the stored gen comes first (not a probe); a fenced copy then fails (b)."""
    m, sim = env
    sim.holders[WS] = (3, sim.now - 1)
    s, _, alarms = new(m, sim, Holder(gen=3, instance_id="1" * 32))
    with pytest.raises(TakeoverRefused):
        s.hold()
    assert "(b)" in alarms[0][1]
    assert holds(m) == [{"gen": 3, "take_over": False}]  # the renew only, no {gen: null}


def test_restored_state_dir_alarms_before_any_probe(env, tmp_path):
    m, sim = env
    st = StateDir(tmp_path / "relay")
    store = FileHolderStore(st)
    store.save(HolderRecord(True, 3, store.instance_id(), None))
    regenerate_instance_id(st)  # what the restore procedure does
    sim.holders[WS] = (3, sim.now - 1)
    s, _, alarms = new(m, sim, store)
    with pytest.raises(TakeoverRefused):
        s.hold()
    assert "(b)" in alarms[0][1] and {"gen": None, "take_over": False} not in holds(m)
    st.close()


def test_answer_without_a_gen_alarms(env):
    m, sim = env
    sim.holders[WS] = (3, sim.now - 1)
    h = Holder(gen=3)
    s, _, alarms = new(m, sim, h)
    s.login()
    m.expect(
        Expect("POST", f"/v2/workspaces/{WS}/host/hold", reply=json_reply(409, {"error": "fenced"})),
        Expect("POST", f"/v2/workspaces/{WS}/host/hold", reply=json_reply(200, {"lease_s": 40, "cursor": 0})),
    )
    with pytest.raises(TakeoverRefused):
        s.hold()
    assert alarms.codes() == ["relay_takeover_refused"] and "no valid gen" in alarms[0][1]
    assert h.rec.pending == 4  # stays: the next start alarms instead of probing


def test_first_acquisition_without_holder_json(env):
    m, sim = env
    h = Holder()
    s, _, _ = new(m, sim, h)
    assert s.hold().gen == 1 and h.rec == HolderRecord(True, 1, IID, None)


def test_operator_takeover_clears_pending_and_proceeds(env):
    m, sim = env
    sim.holders[WS] = (9, sim.now + 40_000)
    h = Holder(gen=3, pending=4)
    s, _, alarms = new(m, sim, h)
    with pytest.raises(TakeoverRefused):
        s.hold()
    got = s.operator_takeover()
    assert got.gen == 10 and h.rec == HolderRecord(True, 10, IID, None)
    assert holds(m)[-1] == {"gen": None, "take_over": True}
    assert s.hold().gen == 10  # renewal of the taken generation


def test_takeover_never_sends_take_over_true_by_itself(env):
    m, sim = env
    for start in [(3, -1), (5, -1), (5, 40_000)]:
        sim.holders[WS] = (start[0], sim.now + start[1])
        s, _, _ = new(m, sim, Holder(gen=3))
        try:
            s.hold()
        except (TakeoverRefused, RelayMisbehaving):
            pass
    assert all(b["take_over"] is False for b in holds(m))


def test_file_holder_store_round_trip(tmp_path):
    st = StateDir(tmp_path / "relay")
    store = FileHolderStore(st)
    iid = store.instance_id()
    assert store.load() is None and len(iid) == 32
    store.save(HolderRecord(True, 5, iid, 6))
    assert store.load() == HolderRecord(True, 5, iid, 6)
    assert FileHolderStore(st).instance_id() == iid  # created once
    assert regenerate_instance_id(st) != iid and store.instance_id() != iid
    st.close()
