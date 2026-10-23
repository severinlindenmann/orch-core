"""R6a under crashes (a fault at every durable step of key creation and the head transitions, and a real SIGKILL of
a child process), and the real key store behind R2's bridge (the loopback seam: ``BridgeKeys`` over
``MembershipKeys``)."""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from orch.identity import pairing
from orch.identity.epochs import CustodyRefused, R6Gate
from orch.relay.bridge import Bridge
from orch.relay.keys import BridgeKeys, MemberList, SealRefused
from orch.relay.rids import RidStore
from orch.wsproto import sign, wire
from orch.wsproto.state import StateDir

from .bridgekit import LIST_HASH, Clock, FakeHost, Phone, Poster, World, open_chunk
from .helpers import WS, Key
from .r6kit import DEVICE, expose_rotation, open_keys, rotate

POINTS = [
    "after_head_create", "before_wk_claim", "after_wk_claim", "after_expose", "after_commit", "after_gate",
    "after_abandon",
]  # fmt: skip


class Crash(Exception):
    pass


def _scenario(m) -> None:
    rotate(m)  # epoch 1: new key, expose, commit
    m.raise_gate(DEVICE, "device.removed")
    m.flush_gate()
    expose_rotation(m)  # epoch 2 exposed
    m.abandon()  # burned
    expose_rotation(m)  # epoch 3 exposed, left exposed


def _keyfiles(tmp: Path) -> dict[str, bytes]:
    d = tmp / "membership" / "keys"
    return {n: (d / n).read_bytes() for n in sorted(os.listdir(d))}


def _invariants(tmp: Path, before: dict[str, bytes]) -> None:
    m = open_keys(tmp)
    try:
        assert m.unsafe is None
        files = _keyfiles(tmp)
        for n, b in before.items():
            assert files[n] == b, "a key file changed: an epoch got a second key"
        held = sorted(int(n[3:-5]) for n in files if n.startswith("wk-"))
        lhw, ehw = m.high_water()
        assert all(e <= ehw for e in held)
        head = m.head()
        ec = head["committed"]["epoch"] if head["committed"] else 0
        pending = head["exposed"]["new_epoch"] if head["exposed"] else None
        for e in held:
            if e > ec and e != pending:
                assert m.is_burned(e), f"wk-{e} above e_c, not exposed, not burned"
        for e in held:
            with pytest.raises(CustodyRefused):
                m.new_epoch_key(e)
        if ec:
            assert head["committed"]["wk_commit"] == pairing.wk_commit(WS, ec, m.wk(ec))
        rotate(m)  # the store keeps working after the crash
    finally:
        m.close()


@pytest.mark.parametrize("point", POINTS)
def test_a_crash_at_every_durable_step_of_key_creation(tmp_path, point):
    hit = {"n": 0}

    def fault(p):
        if p == point and hit["n"] == 0:
            hit["n"] += 1
            raise Crash(p)

    m = None
    try:
        m = open_keys(tmp_path, fault=fault)
        _scenario(m)
    except Crash:
        pass
    finally:
        if m is not None:
            m.close()
    assert hit["n"] == 1, f"{point} never reached"
    _invariants(tmp_path, _keyfiles(tmp_path) if (tmp_path / "membership" / "keys").exists() else {})


def test_a_crash_after_the_orphan_recovery_write_is_idempotent(tmp_path):
    m = open_keys(tmp_path)
    rotate(m)
    m.new_epoch_key(2)
    m.close()
    hit = []

    def fault(p):
        if p == "after_recovery" and not hit:
            hit.append(p)
            raise Crash(p)

    with pytest.raises(Crash):
        open_keys(tmp_path, fault=fault)
    _invariants(tmp_path, _keyfiles(tmp_path))
    assert hit


CHILD = textwrap.dedent(
    """
    import os, signal, sys
    from orch.identity.epochs import MembershipKeys
    root, ws, point = sys.argv[1], sys.argv[2], sys.argv[3]

    def fault(p):
        if p == point:
            os.kill(os.getpid(), signal.SIGKILL)

    m = MembershipKeys(root, ws, now_ms=lambda: 1, fault=fault)
    m.new_epoch_key(1)
    sys.exit(0)
    """
)


@pytest.mark.parametrize("point", ["before_wk_claim", "after_wk_claim"])
def test_a_killed_child_never_leaves_a_reusable_epoch(tmp_path, point):
    r = subprocess.run([sys.executable, "-c", CHILD, str(tmp_path / "membership"), WS, point], timeout=60)
    assert r.returncode == -9
    m = open_keys(tmp_path)
    try:
        assert m.unsafe is None
        if point == "after_wk_claim":
            assert m.is_burned(1) and m.high_water()[1] == 1  # the orphan rule
            with pytest.raises(CustodyRefused):
                m.new_epoch_key(1)
        assert not [n for n in os.listdir(tmp_path / "membership" / "keys") if n.endswith(".tmp")]
    finally:
        m.close()


# -- the real key store behind R2's bridge ---------------------------------------------------------------------------


class View:
    def __init__(self, mk, phone: Phone) -> None:
        self.mk, self.member = mk, phone.member(("look", "decide"))

    def member_list(self) -> MemberList | None:
        e = self.mk.current_epoch()
        return MemberList(e, LIST_HASH, {self.member.device_id: self.member}) if e else None


class RealWorld(World):
    """R2's world with the real ``MembershipKeys`` as its ``EpochKeySource`` (and so its seal lock)."""

    def __init__(self, tmp: Path, mk) -> None:
        self.tmp, self.mk = tmp, mk
        self.phone = Phone()
        self.epochs = mk
        self.r6 = View(mk, self.phone)
        self.clock = Clock()
        self.key = Key()
        self.alarms = []
        (tmp / "host").mkdir(exist_ok=True)
        self.host = FakeHost(tmp / "host", clock=self.clock)
        self.post = Poster()
        self.fault = None
        self.n = 0
        self.state = StateDir(tmp / "relay")
        self.rids = RidStore(self.state)
        keys = BridgeKeys(mk, bytes.fromhex(WS))
        signer = sign.RelaySigner(self.key, sign.LinkContext(WS, "https://relay.example"), epochs=keys)
        self.bridge = Bridge(
            workspace_id=WS, rids=self.rids, keys=keys, signer=signer, r6=self.r6, port=self.host,
            job=lambda fn: fn(), post=self.post, now_ms=self.clock, alarm=lambda c, d: self.alarms.append((c, d)),
        )  # fmt: skip
        self.bridge.start()

    @property
    def wk(self) -> dict[int, bytes]:  # the phone's view: every key it could hold (test only)
        _, ehw = self.mk.high_water()
        return {e: k for e in range(1, ehw + 1) if (k := self.mk.wk(e) or self.mk.refusal_key(e))}


@pytest.fixture
def rw(tmp_path):
    mk = open_keys(tmp_path)
    w = None
    try:
        rotate(mk, devices=[Phone().device])
        w = RealWorld(tmp_path, mk)
        yield w
    finally:
        if w is not None:
            w.close()
        mk.close()


def _meta(w, chunk):
    return open_chunk(chunk, w.wk, w.key.public_key)[1]


def test_a_read_is_answered_under_the_committed_epoch(rw):
    env = rw.phone.request(rw.wk, {"op": "whoami", "args": {}}, epoch=1)
    chunks = rw.send(env)
    assert chunks and _meta(rw, chunks[0]) == {"status": "ok"}


def test_gate_up_stale_epoch_names_target(rw):
    R6Gate(rw.mk).raise_for(rw.phone.device, "device.revoked")
    env = rw.phone.request(rw.wk, {"op": "whoami", "args": {}}, epoch=1)
    chunks = rw.send(env)
    assert [_meta(rw, c) for c in chunks] == [{"refusal": "stale_epoch", "epoch": 2}]


def test_seal_response_refuses_data_under_gated_or_old_epoch(rw):
    env = rw.phone.request(rw.wk, {"op": "whoami", "args": {}}, epoch=1)
    h = wire.parse_header(env[: wire.HEADER_LEN])
    rw.mk.raise_gate(rw.phone.device, "device.removed")
    with rw.bridge.keys.sealing(rw.bridge.signer) as s:
        with pytest.raises(SealRefused):
            s.seal_response(request=h, idx=0, last=True, ts_ms=rw.clock.ms, plaintext=b"x")
        assert s.seal_refusal(request=h, idx=0, ts_ms=rw.clock.ms, code="stale_epoch", epoch=s.state.stale_target)
    rw.mk.flush_gate()  # durable first: a raise still queued keeps the gate up through the commit
    rotate(rw.mk, devices=["ab" * 16])  # e_c = 2, the gate lowered: epoch 1 is old now
    with rw.bridge.keys.sealing(rw.bridge.signer) as s:
        assert not s.state.gated
        with pytest.raises(SealRefused):
            s.seal_response(request=h, idx=0, last=True, ts_ms=rw.clock.ms, plaintext=b"x")


def test_burned_above_named_epoch_is_pending_or_ehw_plus_one(rw):
    _, e2 = expose_rotation(rw.mk)
    rw.mk.abandon()  # burned: now answered stale_epoch, naming EHW + 1
    env = rw.phone.request(rw.wk, {"op": "whoami", "args": {}}, epoch=e2)
    chunks = rw.send(env)
    assert [_meta(rw, c) for c in chunks] == [{"refusal": "stale_epoch", "epoch": e2 + 1}]
    _, e3 = expose_rotation(rw.mk)  # a new pending epoch: the burned one now names it
    env = rw.phone.request(rw.wk, {"op": "whoami", "args": {}}, epoch=e2)
    chunks = rw.send(env)
    assert [_meta(rw, c) for c in chunks] == [{"refusal": "stale_epoch", "epoch": e3}]


def test_a_request_under_an_exposed_uncommitted_epoch_is_dropped(rw):
    _, e2 = expose_rotation(rw.mk)
    wk = rw.mk.refusal_key(e2)
    assert wk is None and rw.mk.wk(e2) is None
    assert not rw.bridge.keys.holds(e2)


def test_the_seal_lock_is_the_key_stores_own(rw):
    """One lock (R6a): BridgeKeys' sealing span and serialised() are R6's seal lock, so the gate raised by the
    store hook waits for an open seal span."""
    import threading

    with rw.bridge.keys.sealing(rw.bridge.signer) as s:
        t = threading.Thread(target=lambda: rw.mk.raise_gate(DEVICE, "device.removed"))
        t.start()
        t.join(0.2)
        # round 4: the in-memory gate goes up at once (fail closed), the queue waits for the seal lock; the open
        # span keeps the snapshot it took before the gate, every later snapshot is gated
        assert t.is_alive() and not s.state.gated and rw.mk.seal_state().gated
    t.join(5)
    assert not t.is_alive() and rw.mk.gate_up() and rw.mk.head()["gate"]["up"]
    with rw.bridge.keys.serialised():
        assert rw.mk.current_epoch() == 1  # reentrant for the source's own reads


def test_after_a_committed_rotation_the_old_epoch_gets_stale_epoch_and_the_new_one_data(rw):
    first = rw.phone.request(rw.wk, {"op": "whoami", "args": {}}, epoch=1)
    rw.send(first)
    rotate(rw.mk, devices=[rw.phone.device])
    rw.bridge.rotate()
    assert len(rw.post.for_rid(first[40:56])) == 1  # an attempted LAST chunk is never replaced
    old = rw.send(rw.phone.request(rw.wk, {"op": "whoami", "args": {}}, epoch=1))
    assert [_meta(rw, c) for c in old] == [{"refusal": "stale_epoch", "epoch": 2}]
    new = rw.send(rw.phone.request(rw.wk, {"op": "whoami", "args": {}}, epoch=2))
    assert _meta(rw, new[0]) == {"status": "ok"}
