"""Crash recovery: kill the writer at each step of the write order; the next lock lands in a consistent state."""

from __future__ import annotations

import multiprocessing as mp

import pytest

from orch import canon
from orch.store import StoreError
from tests.store import workers
from tests.store.helpers import Env, snapshot
from tests.store.test_append import replayed


def crash(env: Env, uid: str, step: str) -> int:
    env.store.close()
    p = mp.get_context("spawn").Process(
        target=workers.crash_at,
        args=(str(env.root), str(env.tmp / "wsk"), str(env.host_state), uid, env.agent, step),
    )
    p.start()
    p.join(60)
    return p.exitcode


def consistent(env: Env, uid: str):
    s = env.open()
    assert s.chain_errors() == [] and s.diverged == {}
    assert s.scan() == []  # files agree with the log: nothing is an external edit
    assert not list((env.root / ".state" / "pending").glob("*")) if (env.root / ".state" / "pending").exists() else True
    fresh = replayed(env)
    assert fresh.tickets[uid].fields == s.state.tickets[uid].fields
    return s


@pytest.fixture
def ws(env):
    env.bootstrap()
    uid = env.new_ticket()
    env.update(uid, body={"context": "before"})
    return env, uid


def test_crash_after_the_pending_files_before_the_append_loses_the_event_cleanly(ws):
    env, uid = ws
    assert crash(env, uid, "after_pending") == 7
    assert (env.root / ".state" / "pending").exists() and list((env.root / ".state" / "pending").iterdir())
    s = consistent(env, uid)
    assert len(env.read_events(uid)) == 2 and s.reports == []
    env.log(uid, "life goes on")


def test_crash_after_the_append_before_the_rename_finishes_the_install(ws):
    env, uid = ws
    assert crash(env, uid, "after_append") == 7
    evs = env.read_events(uid)
    assert evs[-1]["text"] == "dying"
    s = consistent(env, uid)
    assert s.state.tickets[uid] and len(env.read_events(uid)) == 3 and s.reports == []
    assert (env.root / ".state" / "applied").read_bytes()
    env.log(uid, "after")


def test_crash_in_the_middle_of_the_install_is_finished_from_the_pending_files(env):
    env.bootstrap()
    uid = env.new_ticket()
    env.store.close()
    # an update changes ticket.json and body.md: die after the first of them was renamed into place
    s = env.open()
    s.close()
    p = mp.get_context("spawn").Process(
        target=workers.crash_update,
        args=(str(env.root), str(env.tmp / "wsk"), str(env.host_state), uid, env.agent, env.base_rev_for(uid)),
    )
    p.start()
    p.join(60)
    assert p.exitcode == 7
    s = consistent(env, uid)
    assert s.state.tickets[uid].title == "crashed title" and s.reports == []
    assert s.body_sections(uid) == {"context": "crashed body"}
    assert (env.root / ".state" / "body" / f"{uid}.md").read_text() == "## Context\n\ncrashed body\n"


def test_a_torn_line_is_cut_off_and_the_event_is_lost_not_the_log(ws):
    env, uid = ws
    assert crash(env, uid, "torn_line") == 7
    raw = env.path(uid, "events.jsonl").read_bytes()
    assert not raw.endswith(b"\n")  # really torn
    consistent(env, uid)
    assert env.path(uid, "events.jsonl").read_bytes().endswith(b"\n") and len(env.read_events(uid)) == 2
    env.log(uid, "fine")


def test_a_pending_dir_without_manifest_is_deleted(ws):
    env, uid = ws
    d = env.root / ".state" / "pending" / "01J9ZP0000000000000000ZZZZ"
    d.mkdir(parents=True)
    (d / "0").write_bytes(b"half written")
    env.store.close()
    consistent(env, uid)
    assert not d.exists()


def test_files_that_do_not_survive_are_reported_and_answered_as_external_edits(env):
    """After the append the pending copy and the installed file are both gone or wrong: store.torn_write, then the
    ordinary check rebuilds ticket.json and records edit.external for the body.md that is on disk."""
    env.bootstrap()
    uid = env.new_ticket()
    env.store.close()
    p = mp.get_context("spawn").Process(
        target=workers.crash_update,
        args=(str(env.root), str(env.tmp / "wsk"), str(env.host_state), uid, env.agent, {}, "after_append"),
    )
    p.start()
    p.join(60)
    assert p.exitcode == 7
    pend = next((env.root / ".state" / "pending").iterdir())
    for f in pend.iterdir():
        if f.name != "manifest.json":
            f.write_bytes(b"garbage")  # the fsync lied
    s = env.open()
    assert any(r.code == "store.torn_write" for r in s.reports)
    kinds = [(e["type"], e.get("cause")) for e in env.read_events(uid)[2:]]
    assert ("projection.repaired", "projection_mismatch") in kinds
    assert s.state.tickets[uid].title == "crashed title"
    import json

    assert json.loads(env.path(uid, "ticket.json").read_bytes())["title"] == "crashed title"  # rebuilt from events
    assert s.scan() == []


def test_recovery_does_not_double_append_and_keeps_the_chain(ws):
    env, uid = ws
    crash(env, uid, "after_append")
    s = env.open()
    s.close()
    s = env.open()
    seqs = [e["seq"] for e in env.read_events(uid)]
    assert seqs == [1, 2, 3]
    prev = None
    for e in env.read_events(uid):
        assert e["prev"] == prev
        prev = canon.event_head(e)
    before = snapshot(env.root)
    s.scan()
    assert snapshot(env.root) == before


def test_an_exception_after_the_commit_is_recovered_at_the_next_lock(ws, monkeypatch):
    env, uid = ws
    s = env.store
    monkeypatch.setattr(s, "_finish", lambda *a, **k: (_ for _ in ()).throw(OSError("disk full")))
    with pytest.raises(OSError):
        env.log(uid, "committed, not installed")
    monkeypatch.undo()
    assert len(env.read_events(uid)) == 3  # the line is in the log: the append happened
    assert s.state.tickets[uid]  # and the next call (here: reading through a lock) recovers
    env.log(uid, "next")
    assert [e["seq"] for e in env.read_events(uid)] == [1, 2, 3, 4]
    assert not s.chain_errors()
    with pytest.raises(StoreError):
        s.append({"type": "log.added", "actor": env.agent, "text": "x", "seq": 9}, log=uid)
