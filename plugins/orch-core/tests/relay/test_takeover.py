"""The restart takeover decided by orch-52 (13 Oct 2026), core-only and fail closed, against the simulated relay
(whose ``host/hold`` is orch-relay's: a new generation is the row's ``gen + 1``; ``take_over`` steals a live lease)."""

from __future__ import annotations

import pytest

from orch import canon
from orch.relay.session import Fenced, RelayMisbehaving, TakeoverRefused
from orch.wsproto.state import FileHolderStore, HolderRecord, StateDir, regenerate_instance_id

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


def test_other_instance_id_refuses_before_any_network_call(env):
    """R1 approved by orch-52 (13 Oct 2026): (a) and (b) are checked before any network call, the renew included, so a
    copied or restored directory cannot renew into the original's live lease and become a second holder."""
    m, sim = env
    sim.holders[WS] = (3, sim.now + 40_000)  # the original's lease is live
    s, _, alarms = new(m, sim, Holder(gen=3, instance_id="1" * 32))
    with pytest.raises(TakeoverRefused):
        s.hold()
    assert "(b)" in alarms[0][1]
    assert m.seen == []  # no request at all: no login, no renew, no probe


def test_not_the_last_holder_refuses_before_any_network_call(env):
    m, sim = env
    sim.holders[WS] = (3, sim.now + 40_000)
    s, _, alarms = new(m, sim, Holder(gen=3, last_holder=False))
    with pytest.raises(TakeoverRefused):
        s.hold()
    assert "(a)" in alarms[0][1] and m.seen == []


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
    assert "(b)" in alarms[0][1] and m.seen == []
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


# --- the instance record is bound to the held directory's (dev, ino) (orch-52, 13 Oct 2026) -------------------------

import os  # noqa: E402
import shutil  # noqa: E402


def _held_once(path, gen=3):
    st = StateDir(path)
    store = FileHolderStore(st)
    store.save(HolderRecord(True, gen, store.instance_id(), None))
    return st, store


def test_first_creation_records_the_directory_identity(tmp_path):
    st = StateDir(tmp_path / "relay")
    store = FileHolderStore(st)
    obj = st.read_json("instance.json")
    assert (obj["dev"], obj["ino"]) == st.identity() and store.binding_problem() is None
    st.close()


def test_plain_copy_refuses_before_any_network_call(env, tmp_path):
    m, sim = env
    st, _ = _held_once(tmp_path / "relay")
    st.close()
    shutil.copytree(tmp_path / "relay", tmp_path / "copy")  # cp -r: the instance id is copied verbatim
    copy = StateDir(tmp_path / "copy")
    sim.holders[WS] = (3, sim.now + 40_000)
    s, _, alarms = new(m, sim, FileHolderStore(copy))
    with pytest.raises(TakeoverRefused):
        s.hold()
    assert "copied or restored" in alarms[0][1] and m.seen == []
    copy.close()


def test_rename_of_the_same_directory_keeps_working(env, tmp_path):
    m, sim = env
    st, _ = _held_once(tmp_path / "relay")
    st.close()
    os.rename(tmp_path / "relay", tmp_path / "moved")
    moved = StateDir(tmp_path / "moved")
    sim.holders[WS] = (3, sim.now + 40_000)
    s, _, alarms = new(m, sim, FileHolderStore(moved))
    assert s.hold().gen == 3 and alarms == []
    moved.close()


def test_backup_restore_by_recreating_the_directory_refuses(env, tmp_path):
    m, sim = env
    st, _ = _held_once(tmp_path / "relay")
    st.close()
    shutil.copytree(tmp_path / "relay", tmp_path / "backup")
    # keep the old directory (and so its inode) alive while the restored one is created: rmtree + copytree
    # may reuse the freed inode number on ext4/overlayfs (seen in CI), which no local check can tell apart
    os.rename(tmp_path / "relay", tmp_path / "relay.gone")
    shutil.copytree(tmp_path / "backup", tmp_path / "relay")
    shutil.rmtree(tmp_path / "relay.gone")
    os.chmod(tmp_path / "relay", 0o700)
    restored = StateDir(tmp_path / "relay")
    sim.holders[WS] = (3, sim.now - 1)
    s, _, alarms = new(m, sim, FileHolderStore(restored))
    with pytest.raises(TakeoverRefused):
        s.hold()
    assert "copied or restored" in alarms[0][1] and m.seen == []
    restored.close()


def test_regenerate_rebinds_and_the_operator_path_proceeds(env, tmp_path):
    m, sim = env
    st, _ = _held_once(tmp_path / "relay")
    st.close()
    shutil.copytree(tmp_path / "relay", tmp_path / "copy")
    copy = StateDir(tmp_path / "copy")
    store = FileHolderStore(copy)
    old = store.instance_id()
    assert store.binding_problem() is not None
    assert regenerate_instance_id(copy) != old
    assert store.binding_problem() is None  # rebound to this directory
    obj = copy.read_json("instance.json")
    assert (obj["dev"], obj["ino"]) == copy.identity()
    assert sorted(p.name for p in (tmp_path / "copy").iterdir()) == ["holder.json", "instance.json"]  # atomic write
    s, _, alarms = new(m, sim, store)
    with pytest.raises(TakeoverRefused):
        s.hold()  # holder.json is still the original's (b)
    sim.holders[WS] = (3, sim.now - 1)
    assert s.operator_takeover().gen == 4
    assert store.load() == HolderRecord(True, 4, store.instance_id(), None)
    copy.close()


def test_operator_takeover_rebinds_a_copied_directory(env, tmp_path):
    m, sim = env
    st, _ = _held_once(tmp_path / "relay")
    st.close()
    shutil.copytree(tmp_path / "relay", tmp_path / "copy")
    copy = StateDir(tmp_path / "copy")
    store = FileHolderStore(copy)
    sim.holders[WS] = (3, sim.now - 1)
    s, _, _ = new(m, sim, store)
    assert s.operator_takeover().gen == 4
    assert store.binding_problem() is None and store.load().instance_id == store.instance_id()
    copy.close()


# --- st_dev is informational; the binding is the inode (review round 4, N2) -----------------------------------------


def _rebind(st, **change):
    obj = st.read_json("instance.json")
    obj.update(change)
    st.write_json("instance.json", obj)


def test_a_changed_dev_with_the_same_inode_is_not_an_alarm(env, tmp_path, caplog):
    """``st_dev`` is assigned per mount and can change across a reboot: only logged, the host renews its own lease."""
    m, sim = env
    st, store = _held_once(tmp_path / "relay")
    dev, _ = st.identity()
    _rebind(st, dev=dev + 1)
    sim.holders[WS] = (3, sim.now + 40_000)
    s, _, alarms = new(m, sim, store)
    with caplog.at_level("INFO", logger="orch.wsproto.state"):
        assert store.binding_problem() is None
        assert s.hold().gen == 3 and alarms == []
    assert any("device number changed" in r.getMessage() for r in caplog.records)
    st.close()


def test_a_changed_inode_is_an_alarm_before_any_network_call(env, tmp_path):
    m, sim = env
    st, store = _held_once(tmp_path / "relay")
    _, ino = st.identity()
    _rebind(st, ino=ino + 1)
    sim.holders[WS] = (3, sim.now + 40_000)
    s, _, alarms = new(m, sim, store)
    with pytest.raises(TakeoverRefused):
        s.hold()
    assert "copied or restored" in alarms[0][1] and m.seen == []
    st.close()


def test_an_old_format_instance_record_names_the_operator_path(env, tmp_path):
    """A round-3 ``instance.json`` (``{instance_id}`` only) is a hard error naming the operator path, which fixes it."""
    from orch.wsproto.state import StateError

    m, sim = env
    st, store = _held_once(tmp_path / "relay")
    st.write_json("instance.json", {"instance_id": store.load().instance_id})
    s, _, _ = new(m, sim, store)
    with pytest.raises(StateError, match=r"orch host relay takeover.*regenerate_instance_id"):
        s.hold()
    assert m.seen == []
    sim.holders[WS] = (3, sim.now - 1)
    assert s.operator_takeover().gen == 4
    assert store.binding_problem() is None and store.load() == HolderRecord(True, 4, store.instance_id(), None)
    st.close()
