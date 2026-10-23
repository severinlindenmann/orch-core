"""R6a key custody (11A.15.3): files and modes, never reuse an epoch, EHW recovery, the orphan rule, burned epochs,
H7, the EpochKeySource answers, the gate and the seal lock."""

from __future__ import annotations

import json
import logging
import os
import stat
import threading

import pytest

from orch import crypto
from orch.identity import pairing
from orch.identity.epochs import (
    CLAMP,
    FIRST_EPOCH_MAX,
    H7_MARGIN,
    CustodyRefused,
    MembershipUnsafe,
    R6Gate,
)
from orch.relay.keys import SealState
from orch.wsproto.state import StateError

from .helpers import WS
from .r6kit import DEVICE, digest, expose_rotation, list_for, open_keys, rotate


@pytest.fixture
def mk(tmp_path):
    m = open_keys(tmp_path)
    yield m
    m.close()


def _mode(p):
    return stat.S_IMODE(os.lstat(p).st_mode)


def _head(tmp_path):
    return json.loads((tmp_path / "membership" / "head.json").read_bytes())


# -- files, modes, the fresh state ----------------------------------------------------------------------------------


def test_a_fresh_store_has_a_head_private_dirs_and_serves_nothing(tmp_path, mk):
    root = tmp_path / "membership"
    assert _mode(root) == _mode(root / "keys") == _mode(root / "cards") == 0o700
    assert _mode(root / "head.json") == 0o600
    h = _head(tmp_path)
    assert (h["v"], h["lhw"], h["ehw"], h["committed"], h["exposed"], h["gate"]) == (1, 0, 0, None, None, None)
    assert mk.current_epoch() == 0 and mk.wk(1) is None and mk.refusal_key(1) is None
    assert mk.seal_state() == SealState(0, True, 1)  # no data before the first committed list


def test_key_files_are_0600_write_once_and_hold_the_committed_key(tmp_path, mk):
    e = rotate(mk)
    p = tmp_path / "membership" / "keys" / f"wk-{e}.json"
    assert _mode(p) == 0o600
    doc = json.loads(p.read_bytes())
    assert set(doc) == {"v", "epoch", "wk", "created_ms"} and doc["epoch"] == e
    key = mk.wk(e)
    assert crypto.unb64u(doc["wk"]) == key and len(key) == 32
    assert _head(tmp_path)["committed"]["wk_commit"] == pairing.wk_commit(WS, e, key)


def test_a_second_instance_on_the_same_directory_is_refused(tmp_path, mk):
    with pytest.raises(StateError, match="in use"):
        open_keys(tmp_path)


def test_a_wider_key_file_mode_is_refused_never_repaired(tmp_path):
    m = open_keys(tmp_path)
    e = rotate(m)
    m.close()
    p = tmp_path / "membership" / "keys" / f"wk-{e}.json"
    os.chmod(p, 0o644)
    m = open_keys(tmp_path)
    try:
        assert m.unsafe == "key_unreadable" and m.wk(e) is None
        with pytest.raises(MembershipUnsafe):
            m.check_start()
        assert _mode(p) == 0o644  # refused, not repaired
    finally:
        m.close()


def test_a_symlinked_key_file_is_refused(tmp_path):
    m = open_keys(tmp_path)
    e = rotate(m)
    m.close()
    keys = tmp_path / "membership" / "keys"
    os.rename(keys / f"wk-{e}.json", tmp_path / "elsewhere.json")
    os.symlink(tmp_path / "elsewhere.json", keys / f"wk-{e}.json")
    m = open_keys(tmp_path)
    try:
        assert m.unsafe == "key_unreadable"
    finally:
        m.close()


def test_a_crashed_claims_temp_file_is_removed_at_load(tmp_path):
    m = open_keys(tmp_path)
    m.close()
    stray = tmp_path / "membership" / "keys" / ".wk-7.json.0123456789abcdef.tmp"
    stray.write_bytes(b'{"secret": true}')
    m = open_keys(tmp_path)
    m.close()
    assert not stray.exists()


# -- never reuse an epoch -------------------------------------------------------------------------------------------


def test_new_epoch_key_refuses_reuse_the_floor_and_the_clamp(mk):
    with pytest.raises(CustodyRefused):
        mk.new_epoch_key(FIRST_EPOCH_MAX + 1)  # the first list's epoch is at most 2^16
    e = rotate(mk)
    for bad in (e, e - 1, 0, -1, True, "2"):
        with pytest.raises(CustodyRefused):
            mk.new_epoch_key(bad)
    with pytest.raises(CustodyRefused):
        mk.new_epoch_key(e + CLAMP + 1)
    assert mk.new_epoch_key(e + CLAMP)  # the clamp itself is allowed


def test_two_keys_for_one_epoch_are_impossible_even_in_memory(mk):
    mk.new_epoch_key(3)
    with pytest.raises(CustodyRefused):
        mk.new_epoch_key(3)  # the in-memory floor moved with the claim, before any head write
    with pytest.raises(CustodyRefused):
        mk.new_epoch_key(2)


def test_ehw_is_recovered_from_key_files_and_the_signer_ledger(tmp_path):
    m = open_keys(tmp_path)
    rotate(m, 2)
    m.ledger.record("wk_commit", "9", pairing.wk_commit(WS, 9, bytes(32)), "")
    m.ledger.record("member_list", "40", "00" * 32, "sig")
    m.close()
    m = open_keys(tmp_path)
    try:
        assert m.high_water() == (40, 9)
        assert _head(tmp_path)["ehw"] == 9 and _head(tmp_path)["lhw"] == 40  # durable before anything is served
        with pytest.raises(CustodyRefused):
            m.new_epoch_key(9)
        assert m.new_epoch_key(10)
    finally:
        m.close()


# -- the orphan rule, burned epochs ---------------------------------------------------------------------------------


def test_orphan_wk_key_file_counts_as_burned(tmp_path):
    m = open_keys(tmp_path)
    rotate(m)  # e_c = 1
    m.new_epoch_key(2)  # claimed, never exposed: a crash before the head write
    m.close()
    m = open_keys(tmp_path)
    try:
        assert m.is_burned(2) and _head(tmp_path)["burned"]["epochs"] == {"2": 3}
        assert m.wk(2) is None and m.refusal_key(2) is not None  # held for a refusal only (G2b)
        with pytest.raises(CustodyRefused):
            m.new_epoch_key(2)  # never generated again
        assert m.seal_state() == SealState(1, True, 3)  # an orphan above e_c starts gated (round 3, R2)
        with pytest.raises(CustodyRefused):  # never activated
            m.expose(list_seq=2, epoch=2, body=b"x", digest="d", adds=[])
    finally:
        m.close()


def test_a_superseded_rotation_burns_its_epoch_naming_the_next(mk):
    rotate(mk)
    seq, e = expose_rotation(mk)  # epoch 2, exposed
    assert mk.wk(e) is None and mk.refusal_key(e) is None  # exposed, uncommitted: nothing
    assert mk.seal_state().stale_target == e  # the pending epoch
    seq2, e2 = expose_rotation(mk)  # supersedes: list_seq and epoch 2 burned, named 3
    assert mk.is_burned(e) and mk.head()["burned"] == {"epochs": {str(e): e2}, "list_seqs": [seq]}
    assert mk.refusal_key(e) is not None and mk.wk(e) is None


def test_abandon_burns_the_exposed_epoch_and_a_late_2xx_never_activates_it(mk):
    rotate(mk)
    seq, e = expose_rotation(mk)
    mk.abandon()
    o = list_for(e, seq, mk.wk_commit_of(e))
    assert mk.commit(list_seq=seq, epoch=e, list_hash=digest(o), list_obj=o, at_ms=0) is False
    assert mk.current_epoch() == 1 and mk.is_burned(e)
    assert mk.seal_state() == SealState(1, False, e + 1)  # burned above: named EHW + 1


def test_burned_below_the_committed_epoch_is_served_as_old(mk):
    rotate(mk)
    _, e = expose_rotation(mk)
    mk.abandon()
    e3 = rotate(mk)
    assert e3 == e + 1 and mk.wk(e) is not None  # burned below e_c counts as held (old)
    assert mk.refusal_key(e) is not None


def test_commit_refuses_a_list_that_is_not_the_exposed_body(mk):
    rotate(mk)
    seq, e = expose_rotation(mk)
    o = list_for(e, seq, pairing.wk_commit(WS, e, bytes(32)))
    with pytest.raises(CustodyRefused):
        mk.commit(list_seq=seq, epoch=e, list_hash=digest(o), list_obj=o, at_ms=0)
    assert mk.current_epoch() == 1


# -- H7 ---------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("damage", ["delete", "torn", "shape"])
def test_a_lost_or_torn_head_is_h7_and_serves_nothing(tmp_path, damage):
    m = open_keys(tmp_path)
    rotate(m)
    m.close()
    head = tmp_path / "membership" / "head.json"
    if damage == "delete":
        head.unlink()
    elif damage == "torn":
        head.write_bytes(head.read_bytes()[:20])
    else:
        head.write_bytes(json.dumps({**json.loads(head.read_bytes()), "ehw": "x"}).encode())
    m = open_keys(tmp_path)
    try:
        assert m.unsafe in ("head_lost", "head_unreadable", "head_shape")
        assert m.current_epoch() == 0 and m.wk(1) is None and m.refusal_key(1) is None
        assert m.seal_state().gated
        with pytest.raises(MembershipUnsafe):
            m.check_start()
        with pytest.raises(MembershipUnsafe):
            m.new_epoch_key(5)
    finally:
        m.close()


def test_recover_h7_raises_both_marks_by_1024_and_burns_every_held_key(tmp_path):
    m = open_keys(tmp_path)
    rotate(m)
    rotate(m)
    m.close()
    (tmp_path / "membership" / "head.json").unlink()
    m = open_keys(tmp_path)
    try:
        m.recover_h7(relay_lhw=5, relay_ehw=3)
        assert m.unsafe is None
        lhw, ehw = m.high_water()
        assert (lhw, ehw) == (5 + H7_MARGIN, 3 + H7_MARGIN)
        assert m.is_burned(1) and m.is_burned(2) and m.gate_up() and m.current_epoch() == 0
        with pytest.raises(CustodyRefused):
            m.new_epoch_key(ehw)
        e = rotate(m)
        assert e == ehw + 1
    finally:
        m.close()


def test_lhw_recovered_from_ledger_after_lost_head_write(tmp_path):
    """A member list signed (ledger) whose head write was lost: LHW is raised above it at load."""
    m = open_keys(tmp_path)
    rotate(m)
    m.ledger.record("member_list", "7", "11" * 32, "s")
    m.close()
    m = open_keys(tmp_path)
    try:
        assert m.high_water()[0] == 7
        with pytest.raises(CustodyRefused):
            m.expose(list_seq=7, epoch=1, body=b"x", digest="d", adds=[])
    finally:
        m.close()


def test_a_missing_committed_key_or_a_mismatched_commit_refuses_the_start(tmp_path):
    m = open_keys(tmp_path)
    e = rotate(m)
    m.close()
    p = tmp_path / "membership" / "keys" / f"wk-{e}.json"
    doc = json.loads(p.read_bytes())
    os.unlink(p)
    p.write_bytes(json.dumps({**doc, "wk": crypto.b64u(bytes(32))}).encode())
    os.chmod(p, 0o600)
    m = open_keys(tmp_path)
    assert m.unsafe == "wk_commit_mismatch" and m.wk(e) is None
    m.close()
    os.unlink(p)
    m = open_keys(tmp_path)
    assert m.unsafe == "key_missing"
    m.close()


# -- the gate and the seal lock -----------------------------------------------------------------------------------


def test_gate_raised_inside_store_append_before_lock_release(tmp_path, mk):
    """The store hook raises the gate inside the store's critical section: gated, and durable, before the store
    lock is released (round 2, F4: nothing is left for a later flush)."""
    rotate(mk)
    gate = R6Gate(mk)
    store_lock = threading.Lock()
    seen = []
    with store_lock:
        gate.raise_for(DEVICE, "device.removed")
        seen.append(mk.seal_state().gated)
        seen.append(_head(tmp_path)["gate"] is not None)
    assert seen == [True, True]
    g = _head(tmp_path)["gate"]
    assert g["up"] and g["reasons"] == [{"device": DEVICE, "reason": "device.removed"}] and g["target"] == 2
    assert mk.flush_gate() is False


def test_the_gate_survives_a_restart_and_is_lowered_only_by_a_rotation_without_the_device(tmp_path):
    m = open_keys(tmp_path)
    rotate(m)
    R6Gate(m).raise_for(DEVICE, "device.revoked")
    m.flush_gate()
    m.close()
    m = open_keys(tmp_path)
    try:
        assert m.gate_up() and m.seal_state() == SealState(1, True, 2)
        rotate(m, devices=[DEVICE])  # the device is still listed: the gate stays
        assert m.gate_up()
        other = "ab" * 16
        rotate(m, devices=[other])
        assert not m.gate_up() and _head(tmp_path)["gate"] is None
    finally:
        m.close()


def test_a_raise_inside_the_commit_window_keeps_the_gate_up(tmp_path):
    """M27: a raise lands between the commit's durable write and its in-memory lowering (the after_commit point):
    the gate stays up in memory and on disk."""
    hit = []

    def fault(p):
        if p == "after_commit" and not hit:
            hit.append(p)
            m.raise_gate("cd" * 16, "held_evidence")

    m = open_keys(tmp_path, fault=fault)
    try:
        rotate(m)
        m.raise_gate(DEVICE, "device.removed")
        others = ["ef" * 16]
        seq, e = expose_rotation(m, devices=others)  # leaves out the gated device: would lower the gate ...
        o = list_for(e, seq, m.wk_commit_of(e), devices=others)
        hit.clear()
        assert m.commit(list_seq=seq, epoch=e, list_hash=digest(o), list_obj=o, at_ms=0)
        assert hit and m.current_epoch() == e
        assert m.gate_up() and m.head()["gate"]["up"]  # ... but the raise in the window keeps it up
        assert m.head()["gate"]["reasons"][-1] == {"device": "cd" * 16, "reason": "held_evidence"}
    finally:
        m.close()


def test_without_a_queued_raise_the_same_commit_lowers_the_gate(mk):
    rotate(mk)
    mk.raise_gate(DEVICE, "device.removed")
    mk.flush_gate()
    others = ["ef" * 16]
    seq, e = expose_rotation(mk, devices=others)
    o = list_for(e, seq, mk.wk_commit_of(e), devices=others)
    assert mk.commit(list_seq=seq, epoch=e, list_hash=digest(o), list_obj=o, at_ms=0)
    assert not mk.gate_up() and mk.head()["gate"] is None


def test_the_gate_waits_for_an_open_seal_span_and_never_takes_the_store_lock(mk):
    """Lock order: store lock, then seal lock. A seal span in progress finishes first (it is "before the gate");
    the hook only waits for the seal lock, and the seal span never needs the store lock."""
    rotate(mk)
    store_lock = threading.Lock()
    inside, release, order = threading.Event(), threading.Event(), []

    def sealer():
        with mk.seal_snapshot() as st:
            order.append(("sealed under", st.gated))
            inside.set()
            release.wait(5)

    t = threading.Thread(target=sealer)
    t.start()
    assert inside.wait(5)

    def hook():
        with store_lock:
            R6Gate(mk).raise_for(DEVICE, "device.removed")
            order.append("gate raised")

    h = threading.Thread(target=hook)
    h.start()
    h.join(0.2)
    assert h.is_alive() and order == [("sealed under", False)]  # blocked on the seal lock, holding the store lock
    release.set()
    t.join(5)
    h.join(5)
    assert order == [("sealed under", False), "gate raised"]
    with mk.seal_snapshot() as st:
        assert st.gated  # a seal that begins after the gate fails


def test_raise_gate_validates_its_arguments_after_raising_the_gate(mk):
    """Round 4 (F-C1): bad arguments are refused, but the in-memory gate is already up (fail closed)."""
    for dev, why in (("x", "device.removed"), (DEVICE, "nope"), (None, "device.removed")):
        with pytest.raises(ValueError):
            mk.raise_gate(dev, why)
    assert mk.gate_up()


def test_epoch_key_source_answers_per_epoch(mk):
    e1 = rotate(mk)
    e2 = rotate(mk)
    _, e3 = expose_rotation(mk)
    mk.abandon()  # e3 burned above e_c
    assert mk.current_epoch() == e2
    assert mk.wk(e1) and mk.wk(e2) and mk.wk(e3) is None and mk.wk(e3 + 1) is None
    assert mk.refusal_key(e1) and mk.refusal_key(e2) is None and mk.refusal_key(e3)
    mk.raise_gate(DEVICE, "rotate")
    assert mk.refusal_key(e2) == mk.wk(e2)  # e_c under the gate: refusals only
    assert mk.seal_state() == SealState(e2, True, e3 + 1)


def test_nothing_secret_reaches_logs_repr_or_exceptions(tmp_path, caplog):
    caplog.set_level(logging.DEBUG)
    m = open_keys(tmp_path)
    try:
        rotate(m)
        e = rotate(m)
        _, e3 = expose_rotation(m)
        m.abandon()
        texts = [repr(m), str(m.head())]
        for bad in (lambda: m.new_epoch_key(1), lambda: m.expose(list_seq=1, epoch=1, body=b"x", digest="d", adds=[])):
            try:
                bad()
            except Exception as exc:  # noqa: BLE001
                texts.append(str(exc))
        secrets_ = [m.wk(1), m.wk(e), m.refusal_key(e3)]
    finally:
        m.close()
    blob = "\n".join(texts + [r.getMessage() for r in caplog.records])
    for k in secrets_:
        assert crypto.b64u(k) not in blob and k.hex() not in blob and repr(k) not in blob
    assert texts[0] == f"MembershipKeys(workspace={WS}, epoch={e})"


def test_a_late_2xx_for_a_superseded_body_never_activates_it(mk):
    rotate(mk)
    seq_a, e_a = expose_rotation(mk)
    o_a = list_for(e_a, seq_a, mk.wk_commit_of(e_a))
    expose_rotation(mk)  # supersedes A: its list_seq and epoch are burned
    assert mk.commit(list_seq=seq_a, epoch=e_a, list_hash=digest(o_a), list_obj=o_a, at_ms=0) is False
    assert mk.current_epoch() == 1 and mk.is_burned(e_a) and mk.wk(e_a) is None
