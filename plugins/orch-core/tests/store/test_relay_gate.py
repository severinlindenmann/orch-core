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
    assert "_gate_lost_standing(old, new)" in src
