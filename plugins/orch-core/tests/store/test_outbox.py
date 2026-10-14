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
    env.open()
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
    for i in range(3):  # ticket appends too: one entry per append, not one per 50
        env.log(uid, f"n{i}")
    entries = s.outbox_entries()
    assert len(entries) == 4 and all(isinstance(e, OutboxEntry) for e in entries)
    ns = [e.n for e in entries]
    assert ns == sorted(ns) and len(set(ns)) == 4
    for e in entries:
        assert e.kind == "plain" and e.restore is None and not e.catchup
        assert verify_object(env.wsk_pub, e.checkpoint)
        assert e.hash == canon.checkpoint_hash(e.checkpoint["o"]) and e.n == e.checkpoint["o"]["n"]
    last = entries[-1].checkpoint
    assert last == workspace_cp(env)  # the entry is the exact checkpoint the append wrote
    assert last["o"]["tickets"][uid]["seq"] == 4
    assert last["o"]["tickets"][uid]["head"] == s.log_head(uid)
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
    s = env.open()
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
    s = env.open()
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
    s = env.open()
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
