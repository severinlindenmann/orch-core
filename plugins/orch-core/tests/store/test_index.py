"""``.state/index.sqlite`` is derived: incremental equals rebuilt, and a missing, stale or corrupt file is rebuilt."""

from __future__ import annotations

import sqlite3

import pytest

IDX = lambda env: env.root / ".state" / "index.sqlite"  # noqa: E731


@pytest.fixture
def busy(env):
    s = env.bootstrap()
    a, b = env.new_ticket("alpha"), env.new_ticket("beta", "bug")
    env.update(a, {"ticket.labels": ["dbt", "x"], "ticket.priority": "high"})
    env.update(b, {"ticket.size": "s"})
    s.append({"type": "claim.taken", "actor": env.agent}, log=a)
    env.add_device()
    env.close(b)
    return env, s, a, b


def test_incremental_index_equals_a_rebuild(busy):
    env, s, a, b = busy
    inc = s.index.dump()
    s.rebuild_index()
    assert s.index.dump() == inc
    assert len(inc["tickets"]) == 2 and len(inc["members"]) == 1


def test_the_index_answers_the_questions_it_exists_for(busy):
    env, s, a, b = busy
    q = s.index.query
    assert q("SELECT key, status FROM tickets ORDER BY key") == [("DEMO-0001", "in_progress"), ("DEMO-0002", "closed")]
    assert q("SELECT t.key FROM tickets t JOIN ticket_labels l ON l.uid = t.uid WHERE l.label = ?", ["dbt"]) == [
        ("DEMO-0001",)
    ]
    assert q("SELECT count(*) FROM ticket_people WHERE role='owner' AND person=?", [env.owner.ref]) == [(2,)]
    assert q("SELECT priority FROM tickets WHERE uid=?", [a]) == [("high",)]


def test_a_missing_index_is_rebuilt_on_open(busy):
    env, s, a, b = busy
    want = s.index.dump()
    s.close()
    IDX(env).unlink()
    s2 = env.open()
    assert IDX(env).exists() and s2.index.dump() == want


def test_a_stale_index_is_rebuilt_on_open(busy):
    env, s, a, b = busy
    want = s.index.dump()
    s.close()
    db = sqlite3.connect(IDX(env))
    db.execute("UPDATE logs SET seq = seq - 1")
    db.execute("UPDATE tickets SET status = 'done'")
    db.commit()
    db.close()
    assert env.open().index.dump() == want


def test_a_corrupt_index_is_rebuilt_and_never_believed(busy):
    env, s, a, b = busy
    want = s.index.dump()
    s.close()
    IDX(env).write_bytes(b"this is not sqlite" * 100)
    s2 = env.open()
    assert s2.index.dump() == want


def test_the_index_never_decides_anything(busy):
    env, s, a, b = busy
    db = sqlite3.connect(IDX(env))
    db.execute("UPDATE tickets SET status = 'done', owner = 'p_evil'")
    db.commit()
    db.close()
    assert s.state.tickets[a].status == "in_progress"  # state comes from the logs only
    env.log(a)
    s.rebuild_index()
    assert s.index.query("SELECT status FROM tickets WHERE uid=?", [a]) == [("in_progress",)]


def test_a_failing_index_does_not_fail_the_append(busy, monkeypatch):
    env, s, a, b = busy

    def boom(*args, **kw):
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(s.index, "update", boom)
    env.log(a, "still appended")
    assert env.read_events(a)[-1]["text"] == "still appended"
    monkeypatch.undo()
    s.close()
    assert env.open().index.dump()["logs"]  # the stale index is rebuilt at the next open
