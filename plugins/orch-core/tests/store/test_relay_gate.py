"""R6a round 3: THE RELAY GATE INVARIANT, end to end through the real store. A device whose F1 standing goes from
``ok`` to not ``ok``, by any means, calls ``Store.relay_gate`` before the store lock is released: an append of
``device.removed``, ``device.revoked``, ``member.removed`` or a ``restore`` carrying a new revocation, a foreign
append the store reloads, and a held forwarded revocation (``HostPins``). A structural test pins the one chokepoint:
the model state changes only through the ``_state`` setter."""

from __future__ import annotations

import ast
import json
import threading
from pathlib import Path

import pytest

from orch import crypto
from orch.host import admission
from orch.identity import certs
from orch.store import store as store_mod
from tests.identity.helpers import Person
from tests.store.helpers import Env
from tests.store.test_forwarded_revocation import item


@pytest.fixture
def env(tmp_path):
    e = Env(tmp_path)
    e.bootstrap()
    try:
        yield e
    finally:
        if e.store is not None:
            e.store.close()


class Recorder:
    """A relay gate that also records whether it ran under the store's lock (``RLock`` owned by this thread)."""

    def __init__(self, store) -> None:
        self.calls: list[tuple[str, str]] = []
        self.locked: list[bool] = []
        self.store = store
        store.relay_gate = self

    def __call__(self, dev: str, why: str) -> None:
        self.calls.append((dev, why))
        # the call is inside the critical section: this thread holds the store's FileLock (#564: per-thread stack)
        self.locked.append(bool(self.store._lock._stack()))  # noqa: SLF001


def test_device_removed_append_gates_inside_the_lock(env):
    d2, _ = env.add_device()
    rec = Recorder(env.store)
    env.store.append(env.person_event(env.owner, "workspace", "device.removed", device=d2), log="workspace")
    assert admission.device_standing(env.store, env.owner.ref, d2) == "removed"
    assert rec.calls == [(d2[2:], "f1_standing")] and all(rec.locked)


def test_device_revoked_append_gates(env):
    d2, _ = env.add_device()
    rec = Recorder(env.store)
    env.revoke_device(d2)
    assert rec.calls == [(d2[2:], "f1_standing")]


def test_member_removed_gates_every_device_of_the_person(env):
    """X1: member.removed ends the standing of the member's devices without a device event."""
    other = Person()
    s = env.store
    s.append(
        env.person_event(env.owner, "workspace", "member.added", person=other.ref, name="Mara", role="member",
                         pk_pub=crypto.b64u(other.pk_pub), device_cert=other.cert(created=env.clock[0] * 1000)),
        log="workspace",
    )  # fmt: skip
    rec = Recorder(s)
    s.append(env.person_event(env.owner, "workspace", "member.removed", person=other.ref), log="workspace")
    assert admission.device_standing(s, other.ref, other.device) == "removed"
    assert rec.calls == [(other.device[2:], "f1_standing")]


def test_a_foreign_append_reloaded_by_the_hooked_store_gates(env):
    """X2: a second store (another process) appends device.removed; the hooked store's next lock reloads it."""
    d2, _ = env.add_device()
    s1 = env.store
    rec = Recorder(s1)
    s2 = env.other()
    try:
        s2.append(env.person_event(env.owner, "workspace", "device.removed", device=d2), log="workspace")
    finally:
        s2.close()
    assert rec.calls == []
    assert admission.device_standing(s1, env.owner.ref, d2) == "removed"  # the reload under s1's lock
    assert rec.calls == [(d2[2:], "f1_standing")]


def test_a_restore_carrying_a_new_revocation_of_a_live_device_gates(env):
    """X7: a restore's revocations revoke a live device."""
    p = env.owner
    sig_key, kx_key = crypto.generate_private_key(), crypto.generate_private_key()
    sig_pub = crypto.public_bytes(sig_key)
    cert = certs.make_device_cert(p.pk_pub, p.pk_sign, dk_sig_pub=sig_pub, dk_kx_pub=crypto.public_bytes(kx_key),
                                  label_sealed=b"x", created_ms=env.clock[0] * 1000, expires_ms=None,
                                  scopes_max=["look", "decide"])  # fmt: skip
    ref = certs.device_ref(sig_pub)
    s = env.store
    s.append(env.person_event(p, "workspace", "device.added", device=ref, cert=cert), log="workspace")
    rec = Recorder(s)
    rev = certs.make_revocation(p.pk_pub, p.pk_sign, device_id_hex=ref[2:], revoked_ms=(env.clock[0] + 1) * 1000,
                                reason="compromised")  # fmt: skip
    facts = s.restore_facts("workspace")
    facts["revocations"] = [*facts["revocations"], {"revocation": rev, "device_cert": cert,
                                                    "pk_pub": crypto.b64u(p.pk_pub)}]  # fmt: skip
    s.append(env.person_event(p, "workspace", "restore", **facts, reason="other"), log="workspace")
    assert admission.device_standing(s, p.ref, ref) == "removed"
    assert (ref[2:], "f1_standing") in rec.calls


def test_a_held_forwarded_revocation_gates(env):
    """N02: HostPins evidence is not model state; the held path calls the same gate function."""
    d2, sig_pub = env.add_device()
    rec = Recorder(env.store)
    assert env.store.note_forwarded_revocation(item(env.owner, sig_pub)) == "held"
    assert rec.calls == [(d2[2:], "held_evidence")]


def test_a_forwarded_revocation_for_an_unknown_device_is_held_but_does_not_gate(env):
    rec = Recorder(env.store)
    stray = crypto.public_bytes(crypto.generate_private_key())
    assert env.store.note_forwarded_revocation(item(env.owner, stray)) == "held"  # visible to the owner (R4 ledger)
    assert rec.calls == []  # but no device of the workspace is concerned: nothing to gate


def test_unrelated_appends_and_reloads_do_not_gate(env):
    rec = Recorder(env.store)
    env.add_device()
    env.new_ticket()
    env.store.refresh()
    assert rec.calls == []


def test_a_failing_gate_never_breaks_the_append(env):
    d2, _ = env.add_device()

    def boom(dev, why):
        raise RuntimeError("lock order")

    env.store.relay_gate = boom
    env.store.append(env.person_event(env.owner, "workspace", "device.removed", device=d2), log="workspace")
    assert admission.device_standing(env.store, env.owner.ref, d2) == "removed"


def test_close_clears_the_hook(env):
    Recorder(env.store)
    env.store.close()
    assert env.store.relay_gate is None
    env.store = None


def test_the_gate_runs_from_another_thread_too(env):
    d2, _ = env.add_device()
    rec = Recorder(env.store)
    t = threading.Thread(
        target=lambda: env.store.append(
            env.person_event(env.owner, "workspace", "device.removed", device=d2), log="workspace"
        )
    )
    t.start()
    t.join(30)
    assert rec.calls == [(d2[2:], "f1_standing")]


def test_the_model_state_changes_only_through_the_setter():
    """The one chokepoint, by AST over store.py: ``_state_v`` is written only in ``__init__`` and the ``_state``
    setter, and ``_state`` is assigned only in ``_load`` (open, reload, recovered pending appends) and ``_commit``
    (every append path: append, batches, host appends, relay.submit, restore). A new path that changes the state
    without the setter fails here."""
    src = Path(store_mod.__file__).read_text()
    writes: dict[str, set[str]] = {"_state_v": set(), "_state": set()}
    for fn in ast.walk(ast.parse(src)):
        if not isinstance(fn, ast.FunctionDef):
            continue
        for n in ast.walk(fn):
            targets = (
                n.targets
                if isinstance(n, ast.Assign)
                else [n.target]
                if isinstance(n, (ast.AnnAssign, ast.AugAssign))
                else []
            )
            for t in targets:
                for x in ast.walk(t):
                    if isinstance(x, ast.Attribute) and isinstance(x.value, ast.Name) and x.value.id == "self":
                        if x.attr in writes:
                            writes[x.attr].add(fn.name)
    assert writes == {"_state_v": {"__init__", "_state"}, "_state": {"_load", "_commit"}}, writes
    assert "self._gate_lost_standing(old_state, new_state)" in src  # _commit gates before the durable write
    assert "_gate_lost_standing(old, new)" in src


def test_a_device_that_vanishes_from_the_state_counts_as_lost():
    """A restore that abandons a device.added (the device is no longer in the state at all) loses its standing."""
    from types import SimpleNamespace as NS

    from orch.store.store import Store

    calls = []
    s = Store.__new__(Store)
    s._state_v = s._relay_pregated = None  # noqa: SLF001
    s.auth_gen = 0  # set by __init__, which this test skips
    s.relay_gate = lambda d, r: calls.append((d, r))
    dev = "d_" + "1" * 32
    s._state = NS(workspace=NS(devices={dev: NS(removed=False, revoked=False)}))  # noqa: SLF001
    s._state = NS(workspace=NS(devices={}))  # noqa: SLF001
    assert calls == [("1" * 32, "f1_standing")]


# -- round 4 (Opus delta 2) -----------------------------------------------------------------------------------


def _boom(*a, **k):
    raise OSError("disk")


@pytest.mark.parametrize("where", ["finish", "append_durable_after_write", "write_pending"])
def test_a_commit_failing_after_or_at_the_commit_point_is_gated_before_the_lock_is_released(env, monkeypatch, where):
    """F-A: the gate goes up before the event can become durable; a failure at any later point (or at the pending
    write itself) still released the lock with the gate up."""
    d2, _ = env.add_device()
    rec = Recorder(env.store)
    if where == "finish":
        monkeypatch.setattr(store_mod.Store, "_finish", _boom)
    elif where == "write_pending":
        monkeypatch.setattr(store_mod.Store, "_write_pending", _boom)
    else:
        from orch.store.rootfs import RootFS  # #564: every store write goes through the pinned root

        real = RootFS.append_durable

        def late(self, path, line, **kw):
            real(self, path, line, **kw)
            if kw.get("commit"):
                raise OSError("fsync")

        monkeypatch.setattr(RootFS, "append_durable", late)
    with pytest.raises(OSError):
        env.store.append(env.person_event(env.owner, "workspace", "device.removed", device=d2), log="workspace")
    assert rec.calls == [(d2[2:], "f1_standing")] and all(rec.locked)  # no further lock entry needed


def test_a_failed_commit_with_the_real_key_store_is_gated_at_once(env, tmp_path, monkeypatch):
    """E1b: with R6's key store bound to this store, seal_state is gated right after the failed append."""
    from orch.identity.epochs import R6Gate
    from tests.wsproto.r6kit import open_keys, rotate

    d2, _ = env.add_device()
    (tmp_path / "k").mkdir(mode=0o700)
    mk = open_keys(tmp_path / "k", hooked=False)
    try:
        rotate(mk)
        R6Gate(mk).install(lambda: env.store)
        assert not mk.seal_state().gated
        monkeypatch.setattr(store_mod.Store, "_finish", _boom)
        with pytest.raises(OSError):
            env.store.append(env.person_event(env.owner, "workspace", "device.removed", device=d2), log="workspace")
        assert mk.seal_state().gated
    finally:
        mk.close()


def test_a_successful_commit_gates_once(env):
    d2, _ = env.add_device()
    rec = Recorder(env.store)
    env.store.append(env.person_event(env.owner, "workspace", "device.removed", device=d2), log="workspace")
    env.store.refresh()
    assert rec.calls == [(d2[2:], "f1_standing")]


def test_member_removed_gates_every_device_of_the_person_not_only_the_first(env):
    """S16: a person with two devices."""
    other = Person()
    s = env.store
    s.append(
        env.person_event(env.owner, "workspace", "member.added", person=other.ref, name="Mara", role="member",
                         pk_pub=crypto.b64u(other.pk_pub), device_cert=other.cert(created=env.clock[0] * 1000)),
        log="workspace",
    )  # fmt: skip
    d2, _ = env.add_device(other)
    rec = Recorder(s)
    s.append(env.person_event(env.owner, "workspace", "member.removed", person=other.ref), log="workspace")
    assert sorted(rec.calls) == sorted([(other.device[2:], "f1_standing"), (d2[2:], "f1_standing")])


def test_later_appends_do_not_regate_an_already_removed_device(env):
    """S04: only new losses raise the gate."""
    d2, _ = env.add_device()
    rec = Recorder(env.store)
    env.store.append(env.person_event(env.owner, "workspace", "device.removed", device=d2), log="workspace")
    env.new_ticket()
    env.add_device()
    assert rec.calls == [(d2[2:], "f1_standing")]


def test_a_refused_gate_call_falls_back_to_every_device(env):
    calls = []

    def gate(dev, why):
        calls.append((dev, why))
        if why != "h7":
            raise ValueError("gate reason")

    d2, _ = env.add_device()
    env.store.relay_gate = gate
    env.store.append(env.person_event(env.owner, "workspace", "device.removed", device=d2), log="workspace")
    assert calls == [(d2[2:], "f1_standing"), ("", "h7")]


def test_a_load_budget_rollback_never_restores_a_cleared_hook(env):
    from orch.store.store import LoadBudgetExceeded

    Recorder(env.store)
    with pytest.raises(LoadBudgetExceeded):
        with env.store.load_budget(5):
            env.store.relay_gate = None  # close() meanwhile
            raise LoadBudgetExceeded
    assert env.store.relay_gate is None


def test_dict_writes_only_in_load_budget():
    """The setter is bypassed only by load_budget's rollback (``__dict__.update``), which restores an OLDER state
    and keeps the live hook: fail closed (a raised gate stays raised). Any other ``__dict__`` write fails here."""
    src = Path(store_mod.__file__).read_text()
    users = set()
    for fn in ast.walk(ast.parse(src)):
        if isinstance(fn, ast.FunctionDef):
            for n in ast.walk(fn):
                if isinstance(n, ast.Attribute) and n.attr == "__dict__":
                    users.add(fn.name)
    assert users == {"load_budget"}, users


# -- round 5 (Opus delta 3): a gate reason left by an append that failed before its commit point --------------


def _ok(store, dev: str) -> bool:
    """The standing the prune judges: THE function on the store's current state (no reload)."""
    from orch.store.store import device_standing_ok

    return device_standing_ok(store.state, "d_" + dev)


def _keys_bound_to(env, tmp_path):
    from orch.identity.epochs import R6Gate
    from tests.wsproto.r6kit import open_keys, rotate

    (tmp_path / "k").mkdir(mode=0o700)
    mk = open_keys(tmp_path / "k", hooked=False)
    rotate(mk)
    R6Gate(mk).install(lambda: env.store)
    return mk


def test_e1_a_failed_append_before_the_commit_point_leaves_a_reason_the_live_prune_clears(env, tmp_path, monkeypatch):
    """F-1: the gate was written, the event line never was: the device keeps its standing. A restart keeps the
    reason (durable); the live-store prune (under the store lock) drops it and lowers the gate."""
    d2, _ = env.add_device()
    mk = _keys_bound_to(env, tmp_path)
    try:
        monkeypatch.setattr(store_mod.Store, "_write_pending", _boom)
        with pytest.raises(OSError):
            env.store.append(env.person_event(env.owner, "workspace", "device.removed", device=d2), log="workspace")
        monkeypatch.undo()
        assert admission.device_standing(env.store, env.owner.ref, d2) == "ok"
        assert {"device": d2[2:], "reason": "f1_standing"} in mk.head()["gate"]["reasons"] and mk.seal_state().gated
        with env.store.locked():
            assert mk.prune_stale_reasons(lambda: lambda d: _ok(env.store, d)) == 1
        assert mk.head()["gate"] is None and not mk.seal_state().gated
    finally:
        mk.close()


def test_a_real_removal_is_never_pruned(env, tmp_path):
    d2, _ = env.add_device()
    mk = _keys_bound_to(env, tmp_path)
    try:
        env.store.append(env.person_event(env.owner, "workspace", "device.removed", device=d2), log="workspace")
        with env.store.locked():
            assert mk.prune_stale_reasons(lambda: lambda d: _ok(env.store, d)) == 0
        assert mk.seal_state().gated and mk.head()["gate"]["reasons"] == [{"device": d2[2:], "reason": "f1_standing"}]
    finally:
        mk.close()


def test_the_prune_keeps_other_reasons_and_waits_for_queued_raises(env, tmp_path, monkeypatch):
    d2, _ = env.add_device()
    mk = _keys_bound_to(env, tmp_path)
    try:
        mk.raise_gate(d2[2:], "f1_standing")  # stale: d2 is ok
        mk.raise_gate("", "h7")
        mk.raise_gate("", "f1_standing")  # every device (an unparseable id): never pruned
        mk.raise_gate(d2[2:], "device.removed")  # another kind: never pruned
        with env.store.locked():
            assert mk.prune_stale_reasons(lambda: lambda d: _ok(env.store, d)) == 1
        assert mk.seal_state().gated and len(mk.head()["gate"]["reasons"]) == 3
        on_disk = json.loads((tmp_path / "k" / "membership" / "head.json").read_bytes())
        assert on_disk["gate"] == mk.head()["gate"]  # the prune is durable
        mk.raise_gate(d2[2:], "f1_standing")  # stale again, durable
        real = mk._dir.write_json  # noqa: SLF001
        monkeypatch.setattr(mk._dir, "write_json", _boom)  # noqa: SLF001
        mk.raise_gate("cd" * 16, "f1_standing")  # queued: its durable write failed
        monkeypatch.setattr(mk._dir, "write_json", real)  # noqa: SLF001
        assert mk.prune_stale_reasons(lambda: lambda d: True) == 0  # nothing pruned while a raise is queued
        assert {"device": d2[2:], "reason": "f1_standing"} in mk.head()["gate"]["reasons"]
    finally:
        mk.close()


def test_a_refusal_before_the_write_never_gates(env, monkeypatch):
    """N19: the freshness check (chain.broken) refuses before the raise: no gate call."""
    d2, _ = env.add_device()
    rec = Recorder(env.store)
    monkeypatch.setattr(store_mod, "info_sig", lambda info: ("changed",))
    with pytest.raises(store_mod.StoreError):
        env.store.append(env.person_event(env.owner, "workspace", "device.removed", device=d2), log="workspace")
    assert rec.calls == []


def test_no_setattr_or_vars_writes_on_the_store():
    """F-3: besides ``__dict__`` (load_budget only), no ``setattr(self, ...)``, ``vars(self)`` or
    ``object.__setattr__`` in store.py can change the state behind the setter."""
    src = Path(store_mod.__file__).read_text()
    for n in ast.walk(ast.parse(src)):
        if isinstance(n, ast.Call):
            f = n.func
            name = f.id if isinstance(f, ast.Name) else f.attr if isinstance(f, ast.Attribute) else ""
            if name in ("setattr", "vars", "__setattr__"):
                raise AssertionError(f"{name} at line {n.lineno}")


# -- round 5: no validator differential (one standing function for every gate decision) -----------------------


class _RecordingKeys:
    """The key store as the rebuild sees it: one listed device, its raises and the prune's answer recorded."""

    def __init__(self, dev, person, raised, pruned):
        self.dev, self.person, self.raised, self.pruned = dev, person, raised, pruned

    def listed_devices(self):
        return [(self.dev[2:], self.person[2:])]

    def raise_gate(self, d, r):
        self.raised.append((d, r))

    def prune_stale_reasons(self, standing_now):
        self.pruned.append(standing_now()(self.dev[2:]))
        return 0


def test_every_gate_decision_uses_the_same_standing_function(env, tmp_path):
    """Feed the same states to R2's ``device_standing``, THE function, the store chokepoint's per-device
    function, and the relayr6 start-up rebuild (raise) and prune (drop): every decision agrees. Cases: ok, removed,
    revoked, member removed, unknown, another person's device, a new device after a removal, an expired cert."""
    import types

    from orch.host import relayr6
    from orch.store.store import device_standing_ok

    s = env.store
    owner = env.owner.ref
    ok_dev, _ = env.add_device()
    removed, _ = env.add_device()
    revoked, _ = env.add_device()
    expired, _ = env.add_device(expires_ms=(env.clock[0] + 1) * 1000)
    other = Person()
    s.append(
        env.person_event(env.owner, "workspace", "member.added", person=other.ref, name="Mara", role="member",
                         pk_pub=crypto.b64u(other.pk_pub), device_cert=other.cert(created=env.clock[0] * 1000)),
        log="workspace",
    )  # fmt: skip
    s.append(env.person_event(env.owner, "workspace", "device.removed", device=removed), log="workspace")
    env.revoke_device(revoked)
    s.append(env.person_event(env.owner, "workspace", "member.removed", person=other.ref), log="workspace")
    readded, _ = env.add_device()  # a new device of the same person after the removals
    env.clock[0] += 100  # the expired cert is past its expiry now
    unknown = "d_" + "ab" * 16
    cases = {  # (device, listed person) -> expected ok
        "ok": (ok_dev, owner, True),
        "removed": (removed, owner, False),
        "revoked": (revoked, owner, False),
        "member_removed": (other.device, other.ref, False),
        "unknown": (unknown, owner, False),
        "other_persons_device": (ok_dev, other.ref, False),
        "readded": (readded, owner, True),
        "expired_cert": (expired, owner, True),  # expiry is not standing (R6b desired())
    }
    for name, (dev, person, want) in cases.items():
        r2 = admission.device_standing(s, person, dev) == "ok"
        with s.locked():
            pure = device_standing_ok(s.state, dev, person)
            raised, pruned = [], []

            mk = _RecordingKeys(dev, person, raised, pruned)
            relayr6._rebuild_gate(types.SimpleNamespace(store=s), mk)  # noqa: SLF001
        rebuild_ok = raised == []
        assert r2 == pure == rebuild_ok == pruned[0] == want, (name, r2, pure, rebuild_ok, pruned)
        # the chokepoint asks about the device as its own person's (person-agnostic)
        own = s.state.workspace.devices.get(dev)
        if own is not None:
            assert s._standing_ok(s.state, dev) == device_standing_ok(s.state, dev, own.person)  # noqa: SLF001


def test_no_second_standing_implementation_in_the_relay_gate_paths():
    """Structural: relayr6 decides standing only through device_standing_ok on one snapshot; R2's
    device_standing takes its "ok" from device_standing_ok; the chokepoint's per-device test is device_standing_ok.
    (Limit: host/auth.py and host/human.py judge local socket peers with their own removed/revoked checks; they are
    not relay gate decisions.)"""
    from orch.host import admission, relayr6

    rr = Path(relayr6.__file__).read_text()
    assert "device_standing(" not in rr and ".removed" not in rr and ".revoked" not in rr
    # round 7: in _rebuild_gate the store is used ONLY as store.locked(), store.state and store.lock_held(): any other
    # call (refresh, ticket, device_standing, ...) could re-enter and reload between a judgement and its write
    fn = next(n for n in ast.walk(ast.parse(rr)) if isinstance(n, ast.FunctionDef) and n.name == "_rebuild_gate")
    uses = {n.attr for n in ast.walk(fn) if isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name)
            and n.value.id == "store"}  # fmt: skip
    assert uses <= {"locked", "state", "lock_held"}, uses
    assert "device_standing_ok(state, " in rr and "device_standing_ok(now, " in rr  # the one function, two snapshots
    adm = Path(admission.__file__).read_text()
    assert "device_standing_ok(state, device_id, person_id)" in adm
    src = Path(store_mod.__file__).read_text()
    assert "return device_standing_ok(state, device)" in src
    assert src.count("view.removed") == 1 and src.count("view.revoked") == 1  # only inside device_standing_ok


# -- round 6 (Opus delta 4): the prune is atomic and monotone -------------------------------------------------


def test_x1_a_prune_never_writes_a_stale_copy_over_a_raise_from_a_nested_reload(env, tmp_path):
    """X1: a callback that re-enters the store reloads a foreign removal of d3, which raises and writes d3's reason;
    the prune must then write NOTHING (compare-and-swap), so d3's reason survives and the gate stays up."""
    d2, _ = env.add_device()
    d3, _ = env.add_device()
    mk = _keys_bound_to(env, tmp_path)
    try:
        mk.raise_gate(d2[2:], "f1_standing")  # stale: d2 ok
        fired = []

        def standing(d):
            if not fired:
                fired.append(1)
                s2 = env.other()
                try:
                    s2.append(env.person_event(env.owner, "workspace", "device.removed", device=d3), log="workspace")
                finally:
                    s2.close()
            env.store.refresh()  # re-enters, reloads, raises d3 (and writes it)
            return _ok(env.store, d)

        with env.store.locked():
            assert mk.prune_stale_reasons(lambda: standing) == 0
        assert admission.device_standing(env.store, env.owner.ref, d3) == "removed"
        reasons = mk.head()["gate"]["reasons"]
        assert {"device": d3[2:], "reason": "f1_standing"} in reasons and mk.seal_state().gated
    finally:
        mk.close()


def test_x2_the_rebuild_judges_the_store_after_its_reload(env, tmp_path):
    """A stale reason for d2; another store then really removes d2: the rebuild's lock entry reloads first, so the
    reason is kept (and raised again)."""
    import types

    from orch.host import relayr6

    d2, _ = env.add_device()
    mk = _keys_bound_to(env, tmp_path)
    try:
        mk.raise_gate(d2[2:], "f1_standing")
        s2 = env.other()
        try:
            s2.append(env.person_event(env.owner, "workspace", "device.removed", device=d2), log="workspace")
        finally:
            s2.close()
        relayr6._rebuild_gate(types.SimpleNamespace(store=env.store), mk)  # noqa: SLF001
        assert {"device": d2[2:], "reason": "f1_standing"} in mk.head()["gate"]["reasons"] and mk.seal_state().gated
    finally:
        mk.close()


def test_x3_a_device_listed_as_another_persons_is_never_pruned(env, tmp_path, monkeypatch):
    import types

    from orch.host import relayr6

    d2, _ = env.add_device()
    mk = _keys_bound_to(env, tmp_path)
    try:
        mk.raise_gate(d2[2:], "f1_standing")
        monkeypatch.setattr(mk, "listed_devices", lambda: [(d2[2:], "cd" * 16)])
        relayr6._rebuild_gate(types.SimpleNamespace(store=env.store), mk)  # noqa: SLF001
        assert mk.seal_state().gated and {"device": d2[2:], "reason": "f1_standing"} in mk.head()["gate"]["reasons"]
    finally:
        mk.close()


def test_r03_listed_for_two_persons_is_pruned_only_if_ok_for_both(env, tmp_path, monkeypatch):
    import types

    from orch.host import relayr6

    d2, _ = env.add_device()
    mk = _keys_bound_to(env, tmp_path)
    try:
        mk.raise_gate(d2[2:], "f1_standing")
        owner = env.owner.ref[2:]
        monkeypatch.setattr(mk, "listed_devices", lambda: [(d2[2:], owner), (d2[2:], "cd" * 16)])
        relayr6._rebuild_gate(types.SimpleNamespace(store=env.store), mk)  # noqa: SLF001
        assert {"device": d2[2:], "reason": "f1_standing"} in mk.head()["gate"]["reasons"]
        monkeypatch.setattr(mk, "listed_devices", lambda: [(d2[2:], owner)])
        relayr6._rebuild_gate(types.SimpleNamespace(store=env.store), mk)  # noqa: SLF001
        assert mk.head()["gate"] is None
    finally:
        mk.close()


def test_a_raise_from_another_thread_between_judgement_and_write_stops_the_prune(env, tmp_path):
    import threading

    from orch.identity.epochs import R6Gate
    from tests.wsproto.r6kit import open_keys, rotate

    d2, _ = env.add_device()
    (tmp_path / "k").mkdir(mode=0o700)
    queued = threading.Event()
    other = {}

    def fault(point):
        if point == "before_prune_write":
            t = threading.Thread(target=lambda: mk.raise_gate("cd" * 16, "held_evidence"))
            other["t"] = t
            t.start()
            for _ in range(500):  # the raise queues under the seal lock, then waits for our writer lock
                if mk._gate_queue:  # noqa: SLF001
                    queued.set()
                    break
                threading.Event().wait(0.01)

    mk = open_keys(tmp_path / "k", hooked=False, fault=fault)
    try:
        rotate(mk)
        R6Gate(mk).install(lambda: env.store)
        mk.raise_gate(d2[2:], "f1_standing")  # stale
        assert mk.prune_stale_reasons(lambda: lambda d: True) == 0 and queued.is_set()
        other["t"].join(10)
        reasons = mk.head()["gate"]["reasons"]
        assert {"device": "cd" * 16, "reason": "held_evidence"} in reasons and mk.seal_state().gated
    finally:
        mk.close()


def test_p06_the_in_memory_gate_goes_down_only_after_the_durable_write(env, tmp_path):
    from orch.identity.epochs import R6Gate
    from tests.wsproto.r6kit import open_keys, rotate

    class Crash(Exception):
        pass

    def fault(point):
        if point == "before_prune_write":
            raise Crash(point)

    (tmp_path / "k").mkdir(mode=0o700)
    mk = open_keys(tmp_path / "k", hooked=False, fault=fault)
    try:
        rotate(mk)
        R6Gate(mk).install(lambda: env.store)
        mk.raise_gate("ab" * 16, "f1_standing")
        with pytest.raises(Crash):
            mk.prune_stale_reasons(lambda: lambda d: True)
        assert mk.gate_up() and mk.seal_state().gated and mk.head()["gate"] is not None
    finally:
        mk.close()


def test_the_prune_is_refused_while_unsafe(tmp_path):
    from orch.identity.epochs import MembershipUnsafe
    from tests.wsproto.r6kit import open_keys, rotate

    mk = open_keys(tmp_path)
    rotate(mk)
    mk.close()
    (tmp_path / "membership" / "head.json").unlink()
    mk = open_keys(tmp_path)
    try:
        with pytest.raises(MembershipUnsafe):
            mk.prune_stale_reasons(lambda: lambda d: True)
    finally:
        mk.close()


def test_a_failed_prune_write_leaves_the_gate_up(env, tmp_path, monkeypatch):
    """The in-memory gate goes down only after the durable write succeeded."""
    mk = _keys_bound_to(env, tmp_path)
    try:
        mk.raise_gate("ab" * 16, "f1_standing")
        monkeypatch.setattr(mk._dir, "write_json", _boom)  # noqa: SLF001
        with pytest.raises(OSError):
            mk.prune_stale_reasons(lambda: lambda d: True)
        assert mk.gate_up() and mk.seal_state().gated
    finally:
        mk.close()


# -- round 7 (Opus delta 5, orch-52 review) -----------------------------------------------------------------------


def _wait_queued(mk, timeout=5.0):
    import time

    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if mk._gate_queue:  # noqa: SLF001
            return True
        time.sleep(0.01)
    return False


def test_k07_a_raise_queued_between_the_prune_write_and_its_lowering_keeps_the_gate_up(env, tmp_path):
    import threading

    from orch.identity.epochs import R6Gate
    from tests.wsproto.r6kit import open_keys, rotate

    th = {}

    def fault(point):
        if point == "after_prune":
            t = threading.Thread(target=lambda: mk.raise_gate("cd" * 16, "held_evidence"))
            th["t"] = t
            t.start()
            assert _wait_queued(mk)

    (tmp_path / "k").mkdir(mode=0o700)
    mk = open_keys(tmp_path / "k", hooked=False, fault=fault)
    try:
        rotate(mk)
        R6Gate(mk).install(lambda: env.store)
        mk.raise_gate("ab" * 16, "f1_standing")
        assert mk.prune_stale_reasons(lambda: lambda d: True) == 1
        assert mk.gate_up() and mk.seal_state().gated
        th["t"].join(10)
        assert mk.head()["gate"] is not None and mk.seal_state().gated
    finally:
        mk.close()


@pytest.mark.parametrize("reason", ["held_evidence", "device.removed", "device.revoked", "relay_revocation"])
def test_k16_the_prune_keeps_every_other_reason_for_an_ok_device(env, tmp_path, reason):
    d2, _ = env.add_device()
    mk = _keys_bound_to(env, tmp_path)
    try:
        mk.raise_gate(d2[2:], reason)
        with env.store.locked():
            assert mk.prune_stale_reasons(lambda: lambda d: _ok(env.store, d)) == 0
        assert mk.head()["gate"]["reasons"] == [{"device": d2[2:], "reason": reason}] and mk.seal_state().gated
    finally:
        mk.close()


def test_e5_a_reload_between_the_raises_and_the_prune_is_judged_by_the_prunes_own_snapshot(env, tmp_path, monkeypatch):
    """A nested reload inside the span (after the raise snapshot, before the prune): it raises a fresh reason for d3;
    the prune reads its snapshot only after it holds its writer lock, so d3 (really removed) is kept, gated."""
    import types

    from orch.host import relayr6

    d2, _ = env.add_device()
    d3, _ = env.add_device()
    mk = _keys_bound_to(env, tmp_path)
    try:
        mk.raise_gate(d2[2:], "f1_standing")  # stale

        def listed():
            s2 = env.other()
            try:
                s2.append(env.person_event(env.owner, "workspace", "device.removed", device=d3), log="workspace")
            finally:
                s2.close()
            env.store.refresh()  # a nested reload INSIDE the span: raises d3 and writes the head
            return []

        monkeypatch.setattr(mk, "listed_devices", listed)
        relayr6._rebuild_gate(types.SimpleNamespace(store=env.store), mk)  # noqa: SLF001
        reasons = mk.head()["gate"]["reasons"]
        assert {"device": d3[2:], "reason": "f1_standing"} in reasons and mk.seal_state().gated
        assert {"device": d2[2:], "reason": "f1_standing"} not in reasons  # the stale one is still pruned
    finally:
        mk.close()


def test_the_rebuild_prune_refuses_without_the_store_lock(env, tmp_path, monkeypatch):
    """L1: the prune's snapshot factory asserts the store lock (store, then R6's writer: never the reverse)."""
    import types

    from orch.host import relayr6

    d2, _ = env.add_device()
    mk = _keys_bound_to(env, tmp_path)
    try:
        mk.raise_gate(d2[2:], "f1_standing")
        monkeypatch.setattr(env.store, "lock_held", lambda: False)
        with pytest.raises(RuntimeError, match="store lock"):
            relayr6._rebuild_gate(types.SimpleNamespace(store=env.store), mk)  # noqa: SLF001
        assert mk.seal_state().gated
    finally:
        mk.close()


def test_lock_held_tracks_the_store_lock(env):
    assert not env.store.lock_held()
    with env.store.locked():
        assert env.store.lock_held()
    assert not env.store.lock_held()


def test_the_chokepoint_fields_are_private_to_store_py():
    """L2: ``_state_v`` and ``_relay_pregated`` appear in no other module of src/orch."""
    root = Path(store_mod.__file__).resolve().parents[1]
    users = sorted(
        p.relative_to(root).as_posix()
        for p in root.rglob("*.py")
        if ("_state_v" in p.read_text() or "_relay_pregated" in p.read_text())
    )
    assert users == ["store/store.py"], users


def test_the_prune_takes_its_starting_head_before_its_snapshot(env, tmp_path):
    """A snapshot factory whose own reload raises (and writes) a fresh reason for d3: the prune took its starting
    head first, so the head changed under it and nothing is written; d3's reason survives, gated."""
    from orch.store.store import device_standing_ok

    d2, _ = env.add_device()
    d3, _ = env.add_device()
    mk = _keys_bound_to(env, tmp_path)
    try:
        mk.raise_gate(d2[2:], "f1_standing")  # stale

        def standing_now():
            snap = env.store.state  # before the foreign removal of d3
            s2 = env.other()
            try:
                s2.append(env.person_event(env.owner, "workspace", "device.removed", device=d3), log="workspace")
            finally:
                s2.close()
            env.store.refresh()  # reloads: raises d3 and writes the head
            return lambda d: device_standing_ok(snap, "d_" + d)

        with env.store.locked():
            assert mk.prune_stale_reasons(standing_now) == 0
        assert {"device": d3[2:], "reason": "f1_standing"} in mk.head()["gate"]["reasons"] and mk.seal_state().gated
    finally:
        mk.close()


# -- delta 6 nits (orch-52) ---------------------------------------------------------------------------------------


def test_t1_lock_held_is_false_while_another_thread_holds_the_lock(env):
    import threading

    inside, release = threading.Event(), threading.Event()

    def hold():
        with env.store.locked():
            inside.set()
            release.wait(10)

    t = threading.Thread(target=hold)
    t.start()
    try:
        assert inside.wait(10)
        assert not env.store.lock_held()  # held, but by the other thread
    finally:
        release.set()
        t.join(10)


def test_t2_the_snapshot_factory_never_enters_the_store_lock():
    """The prune's snapshot factory (``standing_now`` in ``_rebuild_gate``) must not call ``store.locked()`` (or any
    re-entering store call): it runs inside R6's writer lock, so entering the store lock there would invert the
    order (store, then writer) and could reload."""
    from orch.host import relayr6

    tree = ast.parse(Path(relayr6.__file__).read_text())
    fn = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "standing_now")
    calls = {
        n.func.attr
        for n in ast.walk(fn)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and isinstance(n.func.value, ast.Name)
        and n.func.value.id == "store"
    }  # fmt: skip
    assert calls <= {"lock_held"}, calls
