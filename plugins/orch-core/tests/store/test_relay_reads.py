"""R3's reads of the store (11A.5): ``Store.relay_segment`` (one staging page's heads between two checkpoint entries,
on the current log or an abandoned branch), ``Store.relay_position`` (the startup classification's facts) and
``Store.relay_ancestor``. Each takes the lock and verifies before it answers."""

from __future__ import annotations

import json
import shutil

import pytest

from orch import canon
from orch.store import StoreError
from orch.store.checkpoints import sign_object
from orch.store.logs import WORKSPACE
from tests.store.helpers import Env
from tests.store.test_checkpoints import rollback
from tests.store.test_outbox import link, workspace_cp

ZERO = "sha256:" + "0" * 64


@pytest.fixture
def grown(env):
    env.bootstrap()
    link(env)
    uid = env.new_ticket()
    for i in range(5):
        env.log(uid, f"n{i}")
    return env, uid


def _entry(env: Env, log: str, seq: int) -> dict:
    s = env.store
    events = env.read_events(log)
    acc = ZERO
    for e in events[:seq]:
        acc = canon.log_acc(acc, canon.event_head(e))
    return {"seq": seq, "head": canon.event_head(events[seq - 1]) if seq else None, "acc": acc} if s else {}


def test_a_segment_of_the_current_log_slices_with_the_accumulator_before_it(grown):
    env, uid = grown
    s = env.store
    after, upto = _entry(env, uid, 1), _entry(env, uid, 6)
    whole = s.relay_segment(uid, after, upto)
    heads = [canon.event_head(e) for e in env.read_events(uid)]
    assert whole.after_seq == 1 and whole.from_acc == after["acc"] and list(whole.heads) == heads[1:6]
    part = s.relay_segment(uid, after, upto, skip=2, limit=2)
    assert part.after_seq == 3 and list(part.heads) == heads[3:5] and part.from_acc == _entry(env, uid, 3)["acc"]
    first = s.relay_segment(uid, {"seq": 0, "acc": ZERO}, upto, limit=1)
    assert first.from_acc == ZERO and list(first.heads) == heads[:1]


def test_a_segment_with_a_wrong_endpoint_or_range_is_refused(grown):
    env, uid = grown
    s = env.store
    after, upto = _entry(env, uid, 2), _entry(env, uid, 4)
    with pytest.raises(StoreError) as e:
        s.relay_segment(uid, after, {**upto, "acc": "sha256:" + "7" * 64})
    assert e.value.code == "chain.diverged"
    for bad in ({"seq": 5, "acc": ZERO}, {"seq": -1, "acc": ZERO}, {"acc": ZERO}):
        with pytest.raises(StoreError) as e:
            s.relay_segment(uid, bad, upto)
        assert e.value.code == "validation.relay"
    with pytest.raises(StoreError) as e:
        s.relay_segment(uid, after, upto, skip=3)
    assert e.value.code == "validation.relay"


def test_a_segment_on_an_abandoned_branch_comes_from_the_recorded_ancestry(env, tmp_path):
    env.bootstrap()
    link(env)
    uid = env.new_ticket()
    env.log(uid, "kept")
    env.store.close()
    backup = tmp_path / "backup"
    shutil.copytree(env.root, backup)
    s = env.open()
    for i in range(3):
        env.log(uid, f"lost-{i}")
    lost = _entry(env, uid, 5)
    kept = _entry(env, uid, 2)
    lost_heads = [canon.event_head(e) for e in env.read_events(uid)][2:5]
    s.close()
    rollback(env, backup)
    s = env.open()
    seg = s.relay_segment(uid, kept, lost)
    assert list(seg.heads) == lost_heads and seg.from_acc == kept["acc"]
    tail = s.relay_segment(uid, kept, lost, skip=1, limit=5)
    assert list(tail.heads) == lost_heads[1:] and tail.from_acc == canon.log_acc(kept["acc"], lost_heads[0])


def test_the_position_of_own_forked_and_foreign_checkpoints(grown):
    env, uid = grown
    s = env.store
    cp = workspace_cp(env)
    pos = s.relay_position(cp)
    assert pos.contained and pos.numbering >= cp["o"]["n"] and not pos.abandoned
    assert pos.current == {"n": cp["o"]["n"], "hash": canon.checkpoint_hash(cp["o"])}
    assert s.relay_position(None).contained
    o = json.loads(json.dumps(cp["o"]))
    o["tickets"][uid]["head"] = "sha256:" + "3" * 64
    assert not s.relay_position(sign_object(env.signer.sign, o)).contained
    with pytest.raises(StoreError) as e:
        s.relay_position({"o": o, "sig": cp["sig"]})
    assert e.value.code == "checkpoint.prefix_invalid"


def test_ancestry_between_two_checkpoints(grown):
    env, uid = grown
    s = env.store
    older = s.outbox_entries()[0].checkpoint
    newer = workspace_cp(env)
    assert s.relay_ancestor(older, newer) and s.relay_ancestor(newer, newer)
    assert not s.relay_ancestor(newer, older)
    o = json.loads(json.dumps(older["o"]))
    o["workspace_log"]["head"] = "sha256:" + "4" * 64
    forked = sign_object(env.signer.sign, o)
    assert not s.relay_ancestor(forked, newer)
    assert _entry(env, WORKSPACE, 1)["seq"] == 1


def test_a_checkpoint_with_the_logs_heads_but_another_accumulator_is_not_contained(grown):
    env, uid = grown
    s = env.store
    o = json.loads(json.dumps(workspace_cp(env)["o"]))
    o["tickets"][uid]["acc"] = "sha256:" + "6" * 64
    assert not s.relay_position(sign_object(env.signer.sign, o)).contained


# -- review round 2 (P1, P2): abandoned/ through pinned fds; bounded, cached branch walks ----------------------------


def _forked(env, uid):
    o = json.loads(json.dumps(workspace_cp(env)["o"]))
    o["tickets"][uid]["head"] = "sha256:" + "3" * 64
    return sign_object(env.signer.sign, o)


def test_a_symlinked_abandoned_directory_is_refused_not_followed(grown, tmp_path):
    env, uid = grown
    outside = tmp_path / "outside"
    outside.mkdir()
    forked = _forked(env, uid)
    (outside / "workspace-1.json").write_bytes(canon.cj_checked(forked))
    ab = env.root / ".state" / "checkpoints" / "abandoned"
    shutil.rmtree(ab, ignore_errors=True)
    ab.symlink_to(outside)
    with pytest.raises(StoreError) as e:
        env.store.relay_position(forked)
    assert e.value.code == "validation.path"


def test_a_symlinked_checkpoints_component_is_refused(grown, tmp_path):
    env, uid = grown
    forked = _forked(env, uid)
    cps = env.root / ".state" / "checkpoints"
    moved = tmp_path / "moved"
    shutil.move(str(cps), str(moved))
    cps.symlink_to(moved)
    with pytest.raises(StoreError) as e:
        env.store.relay_position(forked)
    assert e.value.code == "validation.path"


def test_a_symlinked_abandoned_entry_is_not_evidence_and_a_real_one_is(grown, tmp_path):
    env, uid = grown
    forked = _forked(env, uid)
    ab = env.root / ".state" / "checkpoints" / "abandoned"
    ab.mkdir(parents=True, exist_ok=True)
    target = tmp_path / "elsewhere.json"
    target.write_bytes(canon.cj_checked(forked))
    (ab / "workspace-1.json").symlink_to(target)
    assert not env.store.relay_position(forked).abandoned
    (ab / "workspace-2.json").write_bytes(canon.cj_checked(forked))
    assert env.store.relay_position(forked).abandoned


def test_paging_an_abandoned_branch_walks_it_once(env, tmp_path, monkeypatch):
    env.bootstrap()
    link(env)
    uid = env.new_ticket()
    env.log(uid, "kept")
    env.store.close()
    backup = tmp_path / "backup"
    shutil.copytree(env.root, backup)
    s = env.open()
    for i in range(6):
        env.log(uid, f"lost-{i}")
    lost, kept = _entry(env, uid, 8), _entry(env, uid, 2)
    s.close()
    rollback(env, backup)
    s = env.open()
    walks = []
    real = s._pins.branch  # noqa: SLF001

    def branch(*a):
        walks.append(a)
        return real(*a)

    monkeypatch.setattr(s._pins, "branch", branch)  # noqa: SLF001
    pages = [s.relay_segment(uid, kept, lost, skip=i, limit=2) for i in (0, 2, 4)]
    assert len(walks) == 1
    assert pages[1].from_acc == canon.log_acc(canon.log_acc(kept["acc"], pages[0].heads[0]), pages[0].heads[1])
    import orch.store.store as store_mod

    monkeypatch.setattr(store_mod, "RELAY_BRANCH_MAX", 3)
    with pytest.raises(StoreError) as e:
        s.relay_segment(uid, kept, lost)
    assert e.value.code == "validation.relay"


def test_an_ancestry_walk_above_the_bound_is_refused(grown, monkeypatch):
    env, uid = grown
    s = env.store
    import orch.store.store as store_mod

    older = s.outbox_entries()[0].checkpoint
    newer = workspace_cp(env)
    monkeypatch.setattr(store_mod, "RELAY_BRANCH_MAX", 0)
    with pytest.raises(StoreError) as e:
        s.relay_ancestor(older, newer)
    assert e.value.code == "validation.relay"


def test_ancestry_needs_order_and_a_verified_path(grown):
    """The R1 gate's store check (review delta F4): another head at the same seq, a log behind, a head that is not
    on the recorded path, a path whose fold does not reach the accumulator: none is an ancestor."""
    env, uid = grown
    s = env.store
    older = s.outbox_entries()[0].checkpoint
    newer = workspace_cp(env)

    def resigned(cp, f):
        o = json.loads(json.dumps(cp["o"]))
        f(o)
        return sign_object(env.signer.sign, o)

    same_seq = resigned(older, lambda o: o["workspace_log"].update(head="sha256:" + "1" * 64))
    assert not s.relay_ancestor(same_seq, older)
    behind = resigned(newer, lambda o: o["tickets"][uid].update(seq=1)) if uid in older["o"]["tickets"] else None
    if behind is not None:
        assert not s.relay_ancestor(older, behind)
    off_path = resigned(newer, lambda o: o["tickets"][uid].update(head="sha256:" + "2" * 64))
    assert not s.relay_ancestor(older, off_path)
    bad_acc = resigned(newer, lambda o: o["tickets"][uid].update(acc="sha256:" + "3" * 64))
    assert not s.relay_ancestor(older, bad_acc)
    assert s.relay_ancestor(older, newer)


def test_an_abandoned_entry_that_is_not_a_file_is_skipped(grown):
    env, uid = grown
    forked = _forked(env, uid)
    ab = env.root / ".state" / "checkpoints" / "abandoned"
    (ab / "workspace-1.json").mkdir(parents=True)  # (highest_n's own reader trips on it; this reader skips it)
    (ab / "workspace-2.json").write_bytes(canon.cj_checked(forked))
    found = list(env.store._abandoned_workspace_checkpoints())  # noqa: SLF001
    assert found == [canon.cj_checked(forked)]
