"""R6a round 3: THE RELAY GATE INVARIANT, end to end through the real store. A device whose F1 standing goes from
``ok`` to not ``ok``, by any means, calls ``Store.relay_gate`` before the store lock is released: an append of
``device.removed``, ``device.revoked``, ``member.removed`` or a ``restore`` carrying a new revocation, a foreign
append the store reloads, and a held forwarded revocation (``HostPins``). A structural test pins the one chokepoint:
the model state changes only through the ``_state`` setter."""

from __future__ import annotations

import ast
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
        held = self.store._lock._held  # noqa: SLF001 - the call is inside the critical section (lock depth > 0)
        self.locked.append(held.depth > 0)


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


def test_a_pending_forwarded_revocation_does_not_gate(env):
    rec = Recorder(env.store)
    stray = crypto.public_bytes(crypto.generate_private_key())
    assert env.store.note_forwarded_revocation(item(env.owner, stray)) == "pending"
    assert rec.calls == []


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
        real = store_mod.append_durable

        def late(path, line, **kw):
            real(path, line, **kw)
            raise OSError("fsync")

        monkeypatch.setattr(store_mod, "append_durable", late)
    with pytest.raises(OSError):
        env.store.append(env.person_event(env.owner, "workspace", "device.removed", device=d2), log="workspace")
    assert rec.calls == [(d2[2:], "f1_standing")] and all(rec.locked)  # no further lock entry needed


def test_a_failed_commit_with_the_real_key_store_is_gated_at_once(env, tmp_path, monkeypatch):
    """E1b: with R6's key store bound to this store, seal_state is gated right after the failed append."""
    from orch.relay.r6.keystore import R6Gate
    from tests.relay.r6kit import open_keys, rotate

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


def _keys_bound_to(env, tmp_path):
    from orch.relay.r6.keystore import R6Gate
    from tests.relay.r6kit import open_keys, rotate

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
            assert mk.prune_stale_reasons(lambda d: env.store.device_ok("d_" + d)) == 1
        assert mk.head()["gate"] is None and not mk.seal_state().gated
    finally:
        mk.close()


def test_a_real_removal_is_never_pruned(env, tmp_path):
    d2, _ = env.add_device()
    mk = _keys_bound_to(env, tmp_path)
    try:
        env.store.append(env.person_event(env.owner, "workspace", "device.removed", device=d2), log="workspace")
        with env.store.locked():
            assert mk.prune_stale_reasons(lambda d: env.store.device_ok("d_" + d)) == 0
        assert mk.seal_state().gated and mk.head()["gate"]["reasons"] == [{"device": d2[2:], "reason": "f1_standing"}]
    finally:
        mk.close()


def test_the_prune_keeps_other_reasons_and_waits_for_queued_raises(env, tmp_path, monkeypatch):
    d2, _ = env.add_device()
    mk = _keys_bound_to(env, tmp_path)
    try:
        mk.raise_gate(d2[2:], "f1_standing")  # stale: d2 is ok
        mk.raise_gate("", "h7")
        mk.raise_gate(d2[2:], "device.removed")  # another kind: never pruned
        with env.store.locked():
            assert mk.prune_stale_reasons(lambda d: env.store.device_ok("d_" + d)) == 1
        assert mk.seal_state().gated and len(mk.head()["gate"]["reasons"]) == 2
        real = mk._dir.write_json  # noqa: SLF001
        monkeypatch.setattr(mk._dir, "write_json", _boom)  # noqa: SLF001
        mk.raise_gate(d2[2:], "f1_standing")  # queued: its durable write failed
        monkeypatch.setattr(mk._dir, "write_json", real)  # noqa: SLF001
        assert mk.prune_stale_reasons(lambda d: True) == 0  # nothing pruned while a raise is queued
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
