"""R6a round 2 (Opus review of #554): F1 a rolled-back head starts gated; F2 a lost membership directory of a linked
workspace is H7; F3 commit is bound to the exposed body; F4 the gate is durable before the raise returns, the store
hook, fail closed until it is installed; F5 one lock order, refused rather than deadlocked; F6/F7 and the review's
surviving mutants (M16, M18, M27 in test_r6a_keys, M36, M51, M57, M58)."""

from __future__ import annotations

import ast
import json
import logging
import os
import shutil
import threading
from pathlib import Path

import pytest

from orch import canon, crypto
from orch.identity.epochs import CustodyRefused, MembershipUnsafe, R6Gate
from orch.relay.keys import SealRefused, SealState
from orch.store.store import Store
from orch.wsproto import sign, wire
from orch.wsproto.sign import SignerRefused

from .helpers import WS
from .r6kit import DEVICE, digest, expose_rotation, list_for, open_keys, rotate, signer

OTHER = "ab" * 16


def _head(tmp_path):
    return json.loads((tmp_path / "membership" / "head.json").read_bytes())


def _replace(p: Path, data: bytes) -> None:
    os.unlink(p)
    p.write_bytes(data)
    os.chmod(p, 0o600)


# -- F1: a rolled-back head.json --------------------------------------------------------------------------------


def _two_signed_epochs(tmp_path):
    """Epoch 1 lists DEVICE and OTHER, epoch 2 only DEVICE (OTHER removed); every list signed (ledger). Returns
    the head.json bytes as they were at epoch 1."""
    mk = open_keys(tmp_path)
    sg = signer(mk)
    c1 = mk.new_epoch_key(1)
    o1 = list_for(1, 1, c1, [DEVICE, OTHER])
    sg.member_list(o1)
    mk.expose(list_seq=1, epoch=1, body=canon.dumps(o1), digest=digest(o1), adds=[DEVICE, OTHER])
    assert mk.commit(list_seq=1, epoch=1, list_hash=digest(o1), list_obj=o1, at_ms=1)
    head1 = (tmp_path / "membership" / "head.json").read_bytes()
    R6Gate(mk).raise_for(OTHER, "device.removed")
    c2 = mk.new_epoch_key(2)
    o2 = list_for(2, 2, c2, [DEVICE])
    sg.member_list(o2)
    mk.expose(list_seq=2, epoch=2, body=canon.dumps(o2), digest=digest(o2), adds=[DEVICE])
    assert mk.commit(list_seq=2, epoch=2, list_hash=digest(o2), list_obj=o2, at_ms=1)
    assert not mk.gate_up()
    mk.close()
    return head1


def test_a_rolled_back_head_starts_gated_and_durably_so(tmp_path):
    head1 = _two_signed_epochs(tmp_path)
    _replace(tmp_path / "membership" / "head.json", head1)
    mk = open_keys(tmp_path)
    try:
        assert mk.seal_state().gated and mk.current_epoch() == 1
        assert {"device": "", "reason": "h7"} in _head(tmp_path)["gate"]["reasons"]  # durable before anything
        assert mk.wk(2) is None  # the real current epoch is never re-activated
        with pytest.raises(CustodyRefused):
            mk.new_epoch_key(2)
    finally:
        mk.close()
    mk = open_keys(tmp_path)  # and it stays gated across a restart
    try:
        assert mk.seal_state().gated
    finally:
        mk.close()


def test_a_ledger_list_above_the_heads_lhw_starts_gated(tmp_path):
    mk = open_keys(tmp_path)
    rotate(mk)
    mk.ledger.record("member_list", "5", "11" * 32, "s")  # signed, never exposed (or a rolled-back head)
    mk.close()
    mk = open_keys(tmp_path)
    try:
        assert mk.gate_up() and mk.seal_state().gated and mk.high_water()[0] == 5
    finally:
        mk.close()


def test_a_ledger_epoch_above_e_c_alone_starts_gated(tmp_path):
    """Only the epoch evidence: a wk_commit signed above e_c, no list above the head's LHW."""
    mk = open_keys(tmp_path)
    rotate(mk)
    mk.ledger.record("wk_commit", "3", crypto.b64u(bytes(32)), "")
    mk.close()
    mk = open_keys(tmp_path)
    try:
        assert mk.high_water() == (1, 3) and mk.gate_up() and mk.seal_state().gated
    finally:
        mk.close()


def test_a_clean_restart_is_not_gated(tmp_path):
    mk = open_keys(tmp_path)
    rotate(mk)
    mk.close()
    mk = open_keys(tmp_path)
    try:
        assert not mk.seal_state().gated
    finally:
        mk.close()


# -- F2: a lost membership directory -----------------------------------------------------------------------------


def test_a_lost_membership_directory_of_a_linked_workspace_is_h7(tmp_path):
    mk = open_keys(tmp_path)
    rotate(mk)
    mk.close()
    shutil.rmtree(tmp_path / "membership")
    mk = open_keys(tmp_path, previously_linked=True)
    try:
        assert mk.unsafe == "head_lost"
        with pytest.raises(MembershipUnsafe):
            mk.new_epoch_key(1)  # never a second WK_1
        mk.recover_h7(relay_lhw=1, relay_ehw=1)
        assert mk.high_water()[1] == 1 + 1024
    finally:
        mk.close()


def test_a_never_linked_workspace_starts_fresh(tmp_path):
    mk = open_keys(tmp_path, previously_linked=False)
    try:
        assert mk.unsafe is None
    finally:
        mk.close()


# -- F3: commit is bound to the exposed body ----------------------------------------------------------------------


def test_commit_refuses_any_list_but_the_exposed_body_and_keeps_the_gate(tmp_path):
    mk = open_keys(tmp_path)
    try:
        rotate(mk, devices=[DEVICE])
        R6Gate(mk).raise_for(DEVICE, "device.removed")
        lhw, ehw = mk.high_water()
        e = ehw + 1
        c = mk.new_epoch_key(e)
        exposed = list_for(e, lhw + 1, c, [DEVICE])
        mk.expose(list_seq=lhw + 1, epoch=e, body=canon.dumps(exposed), digest=digest(exposed), adds=[DEVICE])
        lie = list_for(e, 999, c, [OTHER])
        for obj, h in ((lie, digest(exposed)), (exposed, "sha256:" + "0" * 64), (lie, digest(lie))):
            with pytest.raises(CustodyRefused):
                mk.commit(list_seq=lhw + 1, epoch=e, list_hash=h, list_obj=obj, at_ms=1)
        assert mk.gate_up() and mk.current_epoch() == e - 1
        assert mk.commit(list_seq=lhw + 1, epoch=e, list_hash=digest(exposed), list_obj=exposed, at_ms=1)
        assert mk.gate_up()  # the exposed body still lists the gated device
        assert mk.head()["committed"]["list"] == exposed
    finally:
        mk.close()


def test_expose_refuses_a_body_that_is_not_the_list_at_this_seq_epoch_and_key(tmp_path):
    mk = open_keys(tmp_path)
    try:
        c = mk.new_epoch_key(1)
        good = list_for(1, 1, c)
        for body in (b"not json", canon.dumps({"x": 1}), canon.dumps(list_for(1, 2, c)),
                     canon.dumps(list_for(1, 1, crypto.b64u(bytes(32))))):  # fmt: skip
            with pytest.raises(CustodyRefused):
                mk.expose(list_seq=1, epoch=1, body=body, digest="d", adds=[DEVICE])
        signed = {"o": good, "sig": "x"}
        put_body = canon.dumps({"member_list": signed, "grants": []})  # the PUT body form: list_hash of the SIGNED list
        h = "sha256:" + crypto.sha256(canon.dumps(signed)).hex()
        mk.expose(list_seq=1, epoch=1, body=put_body, digest=h, adds=[DEVICE])
        assert mk.commit(list_seq=1, epoch=1, list_hash=h, list_obj=good, at_ms=1)
    finally:
        mk.close()


# -- F4: the gate is durable when the raise returns; the store hook; fail closed until installed ----------------


def test_a_raise_survives_a_crash_right_after_it(tmp_path):
    mk = open_keys(tmp_path)
    rotate(mk, devices=[DEVICE])
    R6Gate(mk).raise_for(DEVICE, "device.removed")
    mk.close()  # no flush_gate: the raise was durable when it returned
    mk = open_keys(tmp_path)
    try:
        assert mk.gate_up() and mk.seal_state().gated
    finally:
        mk.close()


def test_a_failed_durable_write_leaves_the_gate_up_in_memory_and_queued(tmp_path, monkeypatch, caplog):
    mk = open_keys(tmp_path)
    try:
        rotate(mk)
        real = mk._dir.write_json  # noqa: SLF001

        def boom(name, obj):
            raise OSError("disk full")

        monkeypatch.setattr(mk._dir, "write_json", boom)  # noqa: SLF001
        mk.raise_gate(DEVICE, "device.removed")
        assert mk.gate_up() and "not yet durable" in caplog.text
        monkeypatch.setattr(mk._dir, "write_json", real)  # noqa: SLF001
        assert mk.flush_gate() is True and _head(tmp_path)["gate"]["up"]
    finally:
        mk.close()


class _FakeStore:
    relay_gate = None


def test_the_key_store_is_gated_unless_the_live_store_carries_its_hook(tmp_path):
    """Round 3 R3: bound to the live store at every decision, never a latch. A replaced or closed store (no hook)
    gates; a rebuild that raises leaves it unbound (gated)."""
    mk = open_keys(tmp_path, hooked=False)
    try:
        rotate(mk)
        assert mk.seal_state() == SealState(1, True, 2)  # never installed
        holder = {"store": _FakeStore()}
        R6Gate(mk).install(lambda: holder["store"])
        assert holder["store"].relay_gate is not None and not mk.seal_state().gated
        holder["store"] = _FakeStore()  # the workspace reopened: a new store without the hook
        assert mk.seal_state().gated
        R6Gate(mk).install(lambda: holder["store"])
        assert not mk.seal_state().gated
        holder["store"].relay_gate = None  # Store.close clears it
        assert mk.seal_state().gated
        holder["store"] = None
        assert mk.seal_state().gated
        with pytest.raises(MembershipUnsafe):
            R6Gate(mk).install(lambda: None)
    finally:
        mk.close()


def test_a_rebuild_that_raises_leaves_the_key_store_gated(tmp_path):
    mk = open_keys(tmp_path, hooked=False)
    try:
        rotate(mk)
        store = _FakeStore()

        def boom():
            raise RuntimeError("store unavailable")

        with pytest.raises(RuntimeError):
            R6Gate(mk).install(lambda: store, rebuild=boom)
        assert store.relay_gate is not None and mk.seal_state().gated  # hooked for events, not bound
    finally:
        mk.close()


def test_expose_refuses_a_digest_that_is_not_the_bodys(tmp_path):
    mk = open_keys(tmp_path)
    try:
        c = mk.new_epoch_key(1)
        o = list_for(1, 1, c)
        with pytest.raises(CustodyRefused, match="digest"):
            mk.expose(list_seq=1, epoch=1, body=canon.dumps(o), digest="sha256:" + "0" * 64, adds=[DEVICE])
        mk.expose(list_seq=1, epoch=1, body=canon.dumps(o), digest=digest(o), adds=[DEVICE])
    finally:
        mk.close()


# -- F5: one lock order; refused, never deadlocked ---------------------------------------------------------------


def test_a_writer_method_inside_serialised_is_refused_not_deadlocked(tmp_path):
    mk = open_keys(tmp_path)
    try:
        seq, e = expose_rotation(mk)
        o = list_for(e, seq, mk.wk_commit_of(e))
        with mk.serialised():
            with pytest.raises(RuntimeError, match="lock order"):
                mk.commit(list_seq=seq, epoch=e, list_hash=digest(o), list_obj=o, at_ms=1)
            with pytest.raises(RuntimeError, match="lock order"):
                mk.raise_gate(DEVICE, "device.removed")
            assert mk.gate_up()  # the in-memory gate went up before the durable part was refused
        assert mk.flush_gate() is True  # the queued raise is written once the lock order allows it
    finally:
        mk.close()


def test_two_threads_writer_then_seal_and_seal_then_writer_never_deadlock(tmp_path):
    """Thread B holds the writer lock (inside flush_gate) and wants the seal lock; thread A holds the seal lock
    (serialised) and asks for a writer method: A is refused at once, B finishes."""
    a_has_seal, b_in_write = threading.Event(), threading.Event()

    def fault(point):
        if point == "after_gate" and threading.current_thread().name == "B":
            b_in_write.set()
            a_has_seal.wait(5)

    mk = open_keys(tmp_path, fault=fault)
    try:
        seq, e = expose_rotation(mk)
        o = list_for(e, seq, mk.wk_commit_of(e))
        out = {}

        def b():
            mk.raise_gate(DEVICE, "rotate")
            out["b"] = True

        def a():
            b_in_write.wait(5)
            with mk.serialised():
                a_has_seal.set()
                try:
                    mk.commit(list_seq=seq, epoch=e, list_hash=digest(o), list_obj=o, at_ms=1)
                except RuntimeError:
                    out["a"] = "refused"

        tb, ta = threading.Thread(target=b, name="B"), threading.Thread(target=a, name="A")
        tb.start()
        ta.start()
        ta.join(10)
        tb.join(10)
        assert not ta.is_alive() and not tb.is_alive(), "deadlock"
        assert out == {"a": "refused", "b": True}
    finally:
        mk.close()


def test_serialised_holds_the_seal_lock(tmp_path):
    """M18: a thread inside serialised() blocks every seal snapshot."""
    mk = open_keys(tmp_path)
    try:
        rotate(mk)
        got = []
        with mk.serialised():
            t = threading.Thread(target=lambda: got.append(mk.seal_state()))
            t2 = threading.Thread(target=lambda: mk.seal_snapshot().__enter__())
            t2.daemon = True
            t2.start()
            t2.join(0.2)
            assert t2.is_alive()  # the snapshot waits for the lock
        t2.join(5)
        t.start()
        t.join(5)
    finally:
        mk.close()


def test_a_forked_child_serves_nothing(tmp_path):
    mk = open_keys(tmp_path)
    try:
        e = rotate(mk)
        r, w = os.pipe()
        pid = os.fork()
        if pid == 0:
            try:
                st = mk.seal_state()
                ok = mk.wk(e) is None and mk.refusal_key(e) is None and st.gated and mk.current_epoch() == 0
                os.write(w, b"1" if ok else b"0")
            finally:
                os._exit(0)
        os.close(w)
        got = os.read(r, 1)
        os.waitpid(pid, 0)
        assert got == b"1"
        assert mk.wk(e) is not None  # the parent is unaffected
    finally:
        mk.close()


# -- F6, F7 ---------------------------------------------------------------------------------------------------


def test_wk_grant_refused_under_a_burned_epoch(tmp_path):
    mk = open_keys(tmp_path)
    try:
        sig = signer(mk)
        rotate(mk)
        lhw, ehw = mk.high_water()
        e = ehw + 1
        o = list_for(e, lhw + 1, mk.new_epoch_key(e), [DEVICE, OTHER])
        sig.member_list(o)
        mk.expose(list_seq=lhw + 1, epoch=e, body=canon.dumps(o), digest=digest(o), adds=[DEVICE, OTHER])
        mk.abandon()
        g = {"v": 2, "suite": crypto.SUITE_ID, "kind": "wk_grant", "workspace_id": WS, "device_id": OTHER,
             "epoch": e, "sealed": crypto.b64u(b"\x01" * (crypto.PUB_LEN + 48))}  # fmt: skip
        with pytest.raises(SignerRefused, match="burned"):
            sig.wk_grant(g, signed_list=o)
    finally:
        mk.close()


def test_expose_refuses_a_lower_claimed_epoch(tmp_path):
    mk = open_keys(tmp_path)
    try:
        rotate(mk)
        mk.new_epoch_key(2)
        mk.new_epoch_key(3)
        lhw, _ = mk.high_water()
        o = list_for(2, lhw + 1, mk.wk_commit_of(2))
        with pytest.raises(CustodyRefused, match="high-water"):
            mk.expose(list_seq=lhw + 1, epoch=2, body=canon.dumps(o), digest=digest(o), adds=[DEVICE])
    finally:
        mk.close()


def test_only_stale_epoch_is_sealed_under_a_burned_epoch_below_e_c(tmp_path):
    from orch.relay.keys import BridgeKeys

    from .bridgekit import Phone
    from .helpers import Key

    mk = open_keys(tmp_path)
    try:
        rotate(mk)
        _, e2 = expose_rotation(mk)
        mk.abandon()
        rotate(mk)  # e_c = 3 > burned 2
        keys = BridgeKeys(mk, bytes.fromhex(WS))
        assert keys.burned(e2) and keys.serves(e2) and not keys.burned(1)
        signer_ = sign.RelaySigner(Key(), sign.LinkContext(WS, "https://relay.example"), epochs=keys)
        env = Phone().request({e2: mk.wk(e2)}, {"op": "whoami", "args": {}}, epoch=e2)
        h = wire.parse_header(env[: wire.HEADER_LEN])
        with keys.sealing(signer_) as s:
            with pytest.raises(SealRefused):
                s.seal_refusal(request=h, idx=0, ts_ms=1, code="already_done", status="unknown")
            assert s.seal_refusal(request=h, idx=0, ts_ms=1, code="stale_epoch", epoch=3)
    finally:
        mk.close()


def test_host_code_never_calls_wk_or_refusal_key():
    """The `.wk(`/`.refusal_key(` ban extended to orch/host (R5 holds the key store there). Limit: value aliases are
    not followed."""
    root = Path(__file__).resolve().parents[2] / "src" / "orch" / "host"
    users = []
    for p in sorted(root.rglob("*.py")):
        for n in ast.walk(ast.parse(p.read_text())):
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr in ("wk", "refusal_key"):
                users.append(p.name)
    assert users == []


# -- the review's surviving mutants --------------------------------------------------------------------------


def test_m16_a_held_key_that_does_not_match_its_ledger_commit_is_unsafe(tmp_path):
    mk = open_keys(tmp_path)
    rotate(mk)
    mk.ledger.record("wk_commit", "1", mk.wk_commit_of(1), "")  # as the R6 signer records it
    rotate(mk)
    mk.close()
    p = tmp_path / "membership" / "keys" / "wk-1.json"  # not the committed epoch: only the ledger pins it
    doc = json.loads(p.read_bytes())
    _replace(p, json.dumps({**doc, "wk": crypto.b64u(bytes(32))}).encode())
    mk = open_keys(tmp_path)
    try:
        assert mk.unsafe == "wk_commit_mismatch"
    finally:
        mk.close()


def test_m36_a_malformed_key_file_never_reaches_an_exception_or_log(tmp_path, caplog):
    caplog.set_level(logging.DEBUG)
    mk = open_keys(tmp_path)
    e = rotate(mk)
    mk.close()
    p = tmp_path / "membership" / "keys" / f"wk-{e}.json"
    doc = json.loads(p.read_bytes())
    _replace(p, json.dumps({**doc, "extra": 1}).encode())
    mk = open_keys(tmp_path)
    try:
        assert mk.unsafe == "key_unreadable"
        with pytest.raises(MembershipUnsafe) as exc:
            mk.check_start()
        blob = str(exc.value) + repr(exc.value) + caplog.text
        assert doc["wk"] not in blob and crypto.unb64u(doc["wk"]).hex() not in blob
    finally:
        mk.close()


def test_m51_a_tampered_stored_card_is_refused_on_resume(tmp_path):
    from orch.identity.epochs import default_part
    from orch.relay.r6.firstcard import R6LinkSource

    from .helpers import OWNER_PK
    from .r6kit import DELEGATION, Clock

    def src(m):
        return R6LinkSource(m, signer(m, "https://relay.example"), delegation=DELEGATION, owner_pk_pub=OWNER_PK,
                            part=default_part("x"), now_ms=Clock(), blocked=lambda: None)  # fmt: skip

    mk = open_keys(tmp_path)
    c = src(mk).card()
    mk.close()
    p = tmp_path / "membership" / "cards" / "card-1.json"
    _replace(p, canon.dumps({**c, "sealed": crypto.b64u(b"\x00" * 40)}))
    mk = open_keys(tmp_path)
    try:
        with pytest.raises(CustodyRefused, match="does not verify"):
            src(mk).card()
    finally:
        mk.close()


def test_m57_a_missing_exposed_rotation_key_is_unsafe(tmp_path):
    mk = open_keys(tmp_path)
    rotate(mk)
    _, e = expose_rotation(mk)
    mk.close()
    os.unlink(tmp_path / "membership" / "keys" / f"wk-{e}.json")
    mk = open_keys(tmp_path)
    try:
        assert mk.unsafe == "key_missing"
    finally:
        mk.close()


def test_m58_genesis_event_is_a_copy():
    s = Store.__new__(Store)
    s._genesis_event = {"type": "workspace.created", "delegation": {"o": {"a": 1}}}  # noqa: SLF001
    g = s.genesis_event()
    g["delegation"]["o"]["a"] = 2
    assert s.genesis_event()["delegation"]["o"]["a"] == 1


# -- round 3 (Opus delta review) -------------------------------------------------------------------------------


def _signed_rotate(mk, devices=None):
    """rotate() with the ledger's wk_commit recorded, as the R6 signer does."""
    lhw, ehw = mk.high_water()
    e = ehw + 1
    c = mk.new_epoch_key(e)
    mk.ledger.record("wk_commit", str(e), c, "")
    o = list_for(e, lhw + 1, c, devices)
    mk.expose(list_seq=lhw + 1, epoch=e, body=canon.dumps(o), digest=digest(o), adds=devices or [DEVICE])
    assert mk.commit(list_seq=lhw + 1, epoch=e, list_hash=digest(o), list_obj=o, at_ms=1)
    return e


def test_r2_head_and_ledger_restored_together_with_newer_keys_start_gated(tmp_path):
    """X3: the orphan key above e_c (the newer keys/) gates the store (h7), as an unaccounted ledger entry does."""
    mk = open_keys(tmp_path)
    _signed_rotate(mk, [DEVICE, OTHER])
    old = {n: (tmp_path / "membership" / n).read_bytes() for n in ("head.json", "sign-ledger.json")}
    _signed_rotate(mk, [DEVICE])
    mk.close()
    for n, data in old.items():
        _replace(tmp_path / "membership" / n, data)
    mk = open_keys(tmp_path)
    try:
        assert mk.current_epoch() == 1 and mk.seal_state().gated and mk.is_burned(2)
        assert {"device": "", "reason": "h7"} in _head(tmp_path)["gate"]["reasons"]
    finally:
        mk.close()


def test_n33_a_clean_restart_with_the_signer_ledger_is_not_gated(tmp_path):
    mk = open_keys(tmp_path)
    _signed_rotate(mk)
    _signed_rotate(mk)
    mk.close()
    mk = open_keys(tmp_path)
    try:
        assert not mk.seal_state().gated
    finally:
        mk.close()


def test_n32_a_restart_while_a_signed_rotation_is_exposed_is_not_gated(tmp_path):
    mk = open_keys(tmp_path)
    _signed_rotate(mk)
    lhw, ehw = mk.high_water()
    c = mk.new_epoch_key(ehw + 1)
    mk.ledger.record("wk_commit", str(ehw + 1), c, "")
    o = list_for(ehw + 1, lhw + 1, c)
    mk.expose(list_seq=lhw + 1, epoch=ehw + 1, body=canon.dumps(o), digest=digest(o), adds=[DEVICE])
    mk.ledger.record("member_list", str(lhw + 1), "00" * 32, "s")
    mk.close()
    mk = open_keys(tmp_path)
    try:
        assert not mk.seal_state().gated and mk.seal_state().stale_target == ehw + 1
    finally:
        mk.close()


def test_listed_devices_include_the_exposed_rotation(tmp_path):
    """X6 (R4): a device added in an exposed rotation is listed for the start-up rebuild."""
    from .r6kit import PERSON

    mk = open_keys(tmp_path)
    try:
        rotate(mk, devices=[DEVICE])
        assert mk.listed_devices() == [(DEVICE, PERSON)]
        expose_rotation(mk, devices=[DEVICE, OTHER])
        assert set(mk.listed_devices()) == {(DEVICE, PERSON), (OTHER, PERSON)}
    finally:
        mk.close()


def test_a_malformed_committed_member_lists_as_every_device(tmp_path):
    """N11 / mutant 18: an entry without ids is listed as ("", "") so the rebuild gates every device."""
    mk = open_keys(tmp_path)
    rotate(mk)
    mk.close()
    p = tmp_path / "membership" / "head.json"
    h = json.loads(p.read_bytes())
    h["committed"]["list"]["members"].append({"scopes": ["look"]})
    _replace(p, json.dumps(h).encode())
    mk = open_keys(tmp_path)
    try:
        assert ("", "") in mk.listed_devices()
    finally:
        mk.close()


# -- round 4 (Opus delta 2) -----------------------------------------------------------------------------------


def test_k03_a_reinstall_whose_rebuild_fails_leaves_the_key_store_gated(tmp_path):
    """The store carries the NEW hook, the key store is still bound to the OLD one: gated (identity check)."""
    mk = open_keys(tmp_path, hooked=False)
    try:
        rotate(mk)
        store = _FakeStore()
        R6Gate(mk).install(lambda: store)
        assert not mk.seal_state().gated

        def boom():
            raise RuntimeError("store unavailable")

        with pytest.raises(RuntimeError):
            R6Gate(mk).install(lambda: store, rebuild=boom)
        assert store.relay_gate is not None and mk.seal_state().gated
    finally:
        mk.close()


def test_k04_a_live_callable_that_raises_is_gated(tmp_path):
    mk = open_keys(tmp_path, hooked=False)
    try:
        rotate(mk)
        store = _FakeStore()
        state = {"raise": False}

        def live():
            if state["raise"]:
                raise RuntimeError("service gone")
            return store

        R6Gate(mk).install(live)
        assert not mk.seal_state().gated
        state["raise"] = True
        assert mk.seal_state().gated
    finally:
        mk.close()


def test_n21_commit_never_lowers_the_durable_gate_while_a_raise_is_queued(tmp_path, monkeypatch):
    """A raise whose durable write failed is queued; a rotation without the gated devices commits: the durable
    gate must stay (the in-memory one too)."""
    mk = open_keys(tmp_path)
    try:
        rotate(mk)
        mk.raise_gate(DEVICE, "device.removed")
        others = [OTHER]
        seq, e = expose_rotation(mk, devices=others)
        real = mk._dir.write_json  # noqa: SLF001

        def once(name, obj):
            monkeypatch.setattr(mk._dir, "write_json", real)  # noqa: SLF001
            raise OSError("disk")

        monkeypatch.setattr(mk._dir, "write_json", once)  # noqa: SLF001
        mk.raise_gate("cd" * 16, "held_evidence")  # its flush fails: queued
        o = list_for(e, seq, mk.wk_commit_of(e), devices=others)
        assert mk.commit(list_seq=seq, epoch=e, list_hash=digest(o), list_obj=o, at_ms=0)
        assert mk.head()["gate"] is not None and mk.gate_up()
    finally:
        mk.close()


def test_the_prune_never_drops_the_every_device_reason(tmp_path):
    mk = open_keys(tmp_path)
    try:
        rotate(mk)
        mk.raise_gate("", "f1_standing")  # the store's raise for an unparseable device id
        assert mk.prune_stale_reasons(lambda: lambda d: True) == 0
        assert mk.seal_state().gated and mk.head()["gate"]["reasons"] == [{"device": "", "reason": "f1_standing"}]
    finally:
        mk.close()
