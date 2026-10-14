"""H12b: the relay outbox hook in the store's append transaction (§11 P3-D21, §11A.3).

In a relay-linked workspace (``<state>/hosts/<ws>/relay/link.json`` exists) every append writes, in the same
transaction, one outbox entry holding the exact workspace checkpoint of that append (for a ``restore``: the target T
and the full evidence set). A crash leaves neither the event nor the entry, or both. R3 reads and deletes entries only
through :meth:`Store.outbox_entries` and :meth:`Store.outbox_delete`.
"""

from __future__ import annotations

import json

import pytest

from orch import canon
from orch.store import StoreError
from orch.store import store as store_mod
from orch.store.checkpoints import verify_object
from orch.store.outbox import OutboxEntry
from tests.store.helpers import WS, Env


class _Crash(BaseException):
    """A crash injected inside the store (BaseException: nothing in the store may swallow it)."""


def _boom(*_a, **_k):
    raise _Crash()


def link(env: Env) -> None:
    d = env.host_state / "hosts" / WS / "relay"
    d.mkdir(parents=True, exist_ok=True)
    (d / "link.json").write_text(json.dumps({"v": 1, "workspace_id": WS, "state": "linking"}))


def outbox_dir(env: Env):
    return env.host_state / "hosts" / WS / "outbox"


def entry_files(env: Env) -> list[str]:
    d = outbox_dir(env)
    return sorted(p.name for p in d.glob("*.json") if p.name[0].isdigit()) if d.is_dir() else []


def workspace_cp(env: Env) -> dict:
    return json.loads((env.root / ".state" / "checkpoints" / "workspace.json").read_bytes())


@pytest.fixture
def linked(env):
    env.bootstrap()
    link(env)
    env.store.close()
    env.open(load="lazy")  # an eager open writes (and queues) a checkpoint of its own
    return env


def test_an_unlinked_workspace_writes_no_outbox(env):
    env.bootstrap()
    uid = env.new_ticket()
    env.log(uid, "x")
    assert not env.store.outbox_active
    assert entry_files(env) == []
    assert env.store.outbox_entries() == []


def test_every_append_writes_its_exact_checkpoint_to_the_outbox(linked):
    env = linked
    s = env.store
    assert s.outbox_active
    uid = env.new_ticket()
    seen = []
    for i in range(3):  # ticket appends too: one entry per append, not one per 50
        env.log(uid, f"n{i}")
        last = s.outbox_entries()[-1]
        assert last.checkpoint == workspace_cp(env)  # the entry is the exact checkpoint the append wrote
        assert last.checkpoint["o"]["tickets"][uid]["seq"] == i + 2
        assert last.checkpoint["o"]["tickets"][uid]["head"] == s.log_head(uid)
        seen.append(last.n)
    assert seen == sorted(seen) and len(set(seen)) == 3
    for e in s.outbox_entries():
        assert isinstance(e, OutboxEntry) and e.kind == "plain" and e.restore is None and not e.catchup
        assert verify_object(env.wsk_pub, e.checkpoint)
        assert e.hash == canon.checkpoint_hash(e.checkpoint["o"]) and e.n == e.checkpoint["o"]["n"]
    mode = (outbox_dir(env) / entry_files(env)[0]).stat().st_mode & 0o777
    assert mode == 0o600 and outbox_dir(env).stat().st_mode & 0o777 == 0o700


def test_a_crash_before_the_commit_leaves_neither_event_nor_entry(linked, monkeypatch):
    env = linked
    uid = env.new_ticket()
    before_entries, before_events = entry_files(env), len(env.read_events(uid))
    monkeypatch.setattr(store_mod, "append_durable", _boom)
    with pytest.raises(_Crash):
        env.log(uid, "dying")
    monkeypatch.undo()
    env.store.close()
    s = env.open(load="lazy")
    assert len(env.read_events(uid)) == before_events
    assert entry_files(env) == before_entries
    assert [e.checkpoint for e in s.outbox_entries()][-1] == workspace_cp(env)
    env.log(uid, "life goes on")
    assert len(entry_files(env)) == len(before_entries) + 1


def test_a_crash_after_the_commit_before_the_install_recovers_both(linked, monkeypatch):
    env = linked
    uid = env.new_ticket()
    n_before = len(entry_files(env))
    monkeypatch.setattr(env.store, "_finish", _boom)
    with pytest.raises(_Crash):
        env.log(uid, "dying")
    assert env.read_events(uid)[-1]["text"] == "dying"  # committed
    assert len(entry_files(env)) == n_before  # not installed yet
    env.store.close()
    s = env.open(load="lazy")
    entries = s.outbox_entries()
    assert len(entries) == n_before + 1
    cp = entries[-1].checkpoint
    assert cp["o"]["tickets"][uid]["seq"] == 2 and cp["o"]["tickets"][uid]["head"] == s.log_head(uid)
    assert cp == workspace_cp(env)  # recovery also adopts the checkpoint as the newest one


def test_a_crash_after_the_install_keeps_the_numbering_above_the_entry(linked, monkeypatch):
    env = linked
    uid = env.new_ticket()
    monkeypatch.setattr(env.store, "_write_checkpoints", _boom)
    with pytest.raises(_Crash):
        env.log(uid, "dying")
    monkeypatch.undo()
    top = env.store.outbox_entries()[-1].n
    env.store.close()
    env.open()
    env.log(uid, "after")
    entries = env.store.outbox_entries()
    assert entries[-1].n > top and len({e.n for e in entries}) == len(entries)


def test_a_forged_pending_entry_is_never_installed(linked, monkeypatch):
    env = linked
    uid = env.new_ticket()
    n_before = len(entry_files(env))
    real = env.store._write_pending

    def forge(ev, log, line, writes, outbox=None):
        assert outbox is not None
        bad = json.loads(outbox)
        bad["checkpoint"]["o"]["tickets"][log]["head"] = "sha256:" + "1" * 64  # signature no longer verifies
        return real(ev, log, line, writes, outbox=canon.cj_checked(bad) + b"\n")

    monkeypatch.setattr(env.store, "_write_pending", forge)
    monkeypatch.setattr(env.store, "_finish", _boom)
    with pytest.raises(_Crash):
        env.log(uid, "dying")
    monkeypatch.undo()
    env.store.close()
    s = env.open(load="lazy")
    assert len(entry_files(env)) == n_before
    assert any(r.code == "store.torn_write" for r in s.reports)


def test_outbox_delete_needs_the_exact_checkpoint(linked):
    env = linked
    s = env.store
    env.new_ticket()
    first = s.outbox_entries()[0]
    with pytest.raises(StoreError) as exc:
        s.outbox_delete(first.n, "sha256:" + "0" * 64)
    assert exc.value.code == "outbox.mismatch"
    assert s.outbox_entries()[0] == first
    assert s.outbox_delete(first.n, first.hash) is True
    assert first.n not in {e.n for e in s.outbox_entries()}
    assert s.outbox_delete(first.n, first.hash) is False  # idempotent: already gone


def test_numbering_stays_above_deleted_entries(linked):
    env = linked
    s = env.store
    uid = env.new_ticket()
    top = 0
    for e in s.outbox_entries():
        assert s.outbox_delete(e.n, e.hash)
        top = max(top, e.n)
    assert entry_files(env) == []
    env.log(uid)
    assert s.outbox_entries()[0].n == top + 1


def test_a_corrupt_entry_on_disk_stops_the_reader(linked):
    env = linked
    env.new_ticket()
    path = outbox_dir(env) / entry_files(env)[-1]
    raw = json.loads(path.read_bytes())
    raw["checkpoint"]["o"]["at"] = "2030-01-01T00:00:00Z"
    path.write_bytes(canon.cj_checked(raw) + b"\n")
    with pytest.raises(StoreError) as exc:
        env.store.outbox_entries()
    assert exc.value.code == "store.torn_write"


def test_an_entry_named_for_another_n_is_refused(linked):
    env = linked
    env.new_ticket()
    path = outbox_dir(env) / entry_files(env)[-1]
    path.rename(outbox_dir(env) / f"{99:020d}.json")
    with pytest.raises(StoreError) as exc:
        env.store.outbox_entries()
    assert exc.value.code == "store.torn_write"


def test_a_restore_writes_a_restore_entry_with_its_evidence(linked):
    env = linked
    s = env.store
    held = s._held_checkpoint()
    facts = s.restore_facts("workspace")
    result = s.append(env.person_event(env.owner, "workspace", "restore", **facts, reason="other"), log="workspace")
    entry = s.outbox_entries()[-1]
    assert entry.kind == "restore"
    assert entry.checkpoint == workspace_cp(env)  # T
    r = entry.restore
    assert r is not None
    assert r["source"] == held  # C
    assert r["event"] == result.event and r["log"] == "workspace"
    assert r["delegation"] == s._genesis_event["delegation"]
    assert r["device_cert"] and r["pk_pub"] and "proofs" in r


def test_a_checkpoint_outside_an_append_is_queued_too(linked):
    env = linked
    env.new_ticket()
    before = len(env.store.outbox_entries())
    env.store.checkpoint()
    entries = env.store.outbox_entries()
    assert len(entries) == before + 1 and entries[-1].checkpoint == workspace_cp(env)


# --- review round 1: the numbering reservation (Opus F1, Codex P1/P2) --------------------------------------------


def _seen_ns(env: Env) -> set[int]:
    """Every checkpoint ``n`` written anywhere on disk: outbox entries, pending entries, the checkpoints, the pin."""
    ns: set[int] = set()
    for p in outbox_dir(env).glob("*.json") if outbox_dir(env).is_dir() else []:
        if p.name[0].isdigit():
            ns.add(json.loads(p.read_bytes())["n"])
    for p in (env.root / ".state" / "pending").glob("*/outbox"):
        ns.add(json.loads(p.read_bytes())["n"])
    for p in (
        env.root / ".state" / "checkpoints" / "workspace.json",
        env.host_state / "hosts" / WS / "checkpoint.json",
    ):
        if p.exists():
            ns.add(json.loads(p.read_bytes())["o"]["n"])
    for p in (env.host_state / "hosts" / WS / "pending-transitions").glob("*.json"):
        ns.add(json.loads(p.read_bytes())["T"]["o"]["n"])
    return ns


def _drain_and_append(env: Env, uid: str) -> int:
    """R3 published and deleted every entry; one more append; its n."""
    s = env.store
    for e in s.outbox_entries():
        s.outbox_delete(e.n, e.hash)
    env.log(uid, "after")
    return s.outbox_entries()[-1].n


def test_poc_f1_a_crash_after_a_queued_checkpoint_never_lets_its_n_be_signed_again(linked, monkeypatch):
    env = linked
    uid = env.new_ticket()
    monkeypatch.setattr(env.store._checkpoints, "write_workspace_object", _boom)
    with pytest.raises(_Crash):
        env.store.checkpoint()  # the outbox entry X is written, workspace.json and the pin are not
    monkeypatch.undo()
    top = max(_seen_ns(env))
    env.store.close()
    env.open(load="lazy")
    assert _drain_and_append(env, uid) > top


@pytest.mark.parametrize("where", ["outbox_write", "accept", "write_ws"])
def test_kill_points_of_a_checkpoint_outside_an_append(linked, monkeypatch, where):
    env = linked
    uid = env.new_ticket()
    from orch.store import outbox as outbox_mod
    from orch.store import pins as pins_mod

    target = {
        "outbox_write": (outbox_mod.Outbox, "write"),
        "accept": (pins_mod.HostPins, "accept_checkpoint"),
        "write_ws": (type(env.store._checkpoints), "write_workspace_object"),
    }[where]
    monkeypatch.setattr(*target, _boom)
    with pytest.raises(_Crash):
        env.store.checkpoint()
    monkeypatch.undo()
    top = max(_seen_ns(env))
    env.store.close()
    env.open(load="lazy")
    assert _drain_and_append(env, uid) > top


@pytest.mark.parametrize("where", ["write_pending", "append_durable", "finish", "accept", "write_ws", "after_commit"])
def test_kill_points_of_a_linked_append(linked, monkeypatch, where):
    env = linked
    uid = env.new_ticket()
    from orch.store import pins as pins_mod

    s = env.store
    if where == "write_pending":
        monkeypatch.setattr(s, "_write_pending", _boom)
    elif where == "append_durable":
        monkeypatch.setattr(store_mod, "append_durable", _boom)
    elif where == "finish":
        monkeypatch.setattr(s, "_finish", _boom)
    elif where == "accept":
        monkeypatch.setattr(pins_mod.HostPins, "accept_checkpoint", _boom)
    elif where == "write_ws":
        monkeypatch.setattr(type(s._checkpoints), "write_workspace_object", _boom)
    else:
        monkeypatch.setattr(s, "_write_checkpoints", _boom)
    with pytest.raises(_Crash):
        env.log(uid, "dying")
    monkeypatch.undo()
    top = max(_seen_ns(env))
    env.store.close()
    env.open(load="lazy")
    assert _drain_and_append(env, uid) > top


def test_poc_codex_p1_recovery_repairs_the_pin_and_workspace_json_independently(linked, monkeypatch):
    env = linked
    uid = env.new_ticket()
    from orch.store import pins as pins_mod

    monkeypatch.setattr(pins_mod.HostPins, "accept_checkpoint", _boom)  # between the two adoption writes
    monkeypatch.setattr(env.store, "_write_checkpoints", _boom)
    with pytest.raises(_Crash):
        env.log(uid, "dying")
    monkeypatch.undo()
    env.store.close()
    s = env.open(load="lazy")
    last = s.outbox_entries()[-1].checkpoint
    assert last["o"]["tickets"][uid]["seq"] == 2
    assert workspace_cp(env) == last
    assert s._pins.held() == last


def test_the_reservation_is_durable_and_never_falls(linked):
    env = linked
    s = env.store
    pins = s._pins
    before = pins.high_n()
    pins.reserve_n(before + 5)
    assert pins.high_n() == before + 5
    pins.reserve_n(before + 1)  # never lowered
    assert pins.high_n() == before + 5
    raw = (env.host_state / "hosts" / WS / "checkpoint-n").read_text()
    assert int(raw) == before + 5
    uid = env.new_ticket()
    assert s.outbox_entries()[-1].n == before + 6
    _ = uid


def test_an_append_does_not_rescan_the_outbox_for_its_number(linked, monkeypatch):
    env = linked
    uid = env.new_ticket()
    from orch.store import outbox as outbox_mod

    def no_scan(self):
        raise AssertionError("max_n scans the whole outbox")

    monkeypatch.setattr(outbox_mod.Outbox, "max_n", no_scan)
    env.log(uid, "x")


# --- review round 1: coalescing (Opus F3) ---------------------------------------------------------------------------


def test_plain_entries_coalesce_to_the_lowest_and_the_newest(linked):
    env = linked
    s = env.store
    uid = env.new_ticket()
    first = s.outbox_entries()[0]
    for i in range(6):
        env.log(uid, f"n{i}")
    entries = s.outbox_entries()
    assert [e.n for e in entries] == [first.n, entries[-1].n]  # the lowest may be staging: it stays
    assert entries[-1].checkpoint == workspace_cp(env)
    assert s.outbox_first() == entries[0]
    assert s.outbox_entries(limit=1) == entries[:1]


def test_coalescing_keeps_restores_their_sources_and_catchup_entries(linked):
    env = linked
    s = env.store
    uid = env.new_ticket()
    env.log(uid, "a")
    env.log(uid, "b")
    source = s.outbox_entries()[-1]
    facts = s.restore_facts("workspace")
    s.append(env.person_event(env.owner, "workspace", "restore", **facts, reason="other"), log="workspace")
    restore = s.outbox_entries()[-1]
    assert restore.kind == "restore" and restore.restore["source"] == source.checkpoint
    for i in range(4):
        env.log(uid, f"after{i}")
    entries = s.outbox_entries()
    kinds = [(e.n, e.kind) for e in entries]
    assert (source.n, "plain") in kinds and (restore.n, "restore") in kinds
    assert len(entries) == 4  # lowest, the restore's source, the restore, the newest
    assert entries[-1].checkpoint == workspace_cp(env)


def test_coalescing_never_removes_catchup_entries(linked):
    env = linked
    s = env.store
    uid = env.new_ticket()
    for i in range(4):
        env.log(uid, f"n{i}")
    from tests.store.test_catchup import measure

    minted = s.mint_catchup(source=None, cap=400, measure=measure)
    env.log(uid, "later")
    env.log(uid, "later2")
    ns = [e.n for e in s.outbox_entries()]
    assert all(e.n in ns for e in minted)


def test_outbox_delete_refuses_a_symlinked_entry(linked, tmp_path):
    env = linked
    env.new_ticket()
    s = env.store
    e = s.outbox_entries()[-1]
    path = outbox_dir(env) / f"{e.n:020d}.json"
    target = tmp_path / "elsewhere.json"
    target.write_bytes(path.read_bytes())
    path.unlink()
    path.symlink_to(target)
    with pytest.raises(StoreError) as exc:
        s.outbox_delete(e.n, e.hash)
    assert exc.value.code == "store.torn_write"


# --- review round 1: F4, F5, F6 ------------------------------------------------------------------------------------


def test_f4_catchup_refuses_a_ticket_without_its_checkpoint(linked):
    env = linked
    a = env.new_ticket()
    env.log(a, "x")
    b = env.new_ticket()
    env.log(b, "y")  # the last append: only b's checkpoint would be repairable
    (env.root / ".state" / "checkpoints" / f"ticket-{a}.json").unlink()
    from tests.store.test_catchup import measure

    env.store.close()
    s = env.open(load="lazy")
    with pytest.raises(StoreError) as exc:
        s.mint_catchup(source=None, cap=10_000, measure=measure)
    assert exc.value.code == "checkpoint.missing"


def test_f5_planning_leaves_out_an_excluded_log(linked):
    env = linked
    s = env.store
    uid = env.new_ticket()
    from orch.store.checkpoints import Divergence

    ev = dict(env.read_events(uid)[-1], seq=2)
    s._diverged[uid] = Divergence(uid, 1, "test")
    try:
        assert s._plan_workspace_checkpoint(uid, ev, s.state) is None
    finally:
        s._diverged.pop(uid)


def test_f5_planning_refuses_over_a_conflicting_ticket_checkpoint(linked):
    env = linked
    s = env.store
    uid = env.new_ticket()
    from orch.store.checkpoints import sign_object

    path = env.root / ".state" / "checkpoints" / f"ticket-{uid}.json"
    o = json.loads(path.read_bytes())["o"]
    o["seq"] = 5  # a ticket checkpoint ahead of the append: the post-commit write would call it diverged
    path.write_bytes(canon.cj_checked(sign_object(env.signer.sign, o)) + b"\n")
    ev = dict(env.read_events(uid)[-1], seq=2)
    assert s._plan_workspace_checkpoint(uid, ev, s.state) is None


def test_f6_the_doctor_names_a_pending_linked_append(linked, monkeypatch):
    env = linked
    uid = env.new_ticket()
    monkeypatch.setattr(env.store, "_finish", _boom)
    with pytest.raises(_Crash):
        env.log(uid, "dying")
    monkeypatch.undo()
    env.store.close()
    env.store = None
    from orch.ops import health

    findings: list = []
    s = health._open(env.root, WS, env.host_state, findings)
    assert s is not None
    s.close()
    hit = [f for f in findings if f.code == "outbox.pending"]
    assert hit and uid in hit[0].what
    with pytest.raises(StoreError) as exc:  # a store without host state names it too, and keeps it for the host
        store_mod.Store.open(env.root, expected_workspace_id=WS, load="lazy")
    assert exc.value.code == "validation.host_state" and uid in exc.value.detail
    assert list((env.root / ".state" / "pending").glob("*/outbox"))
