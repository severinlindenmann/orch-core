"""The first hub step preserves existing data and tolerates crash replay."""

import sqlite3
from contextlib import closing

import pytest

from orch.migrate import DEFAULT, StepContext, apply, read_format, write_format
from orch.ops.errors import OrchError


@pytest.mark.parametrize("user_version", [0, 7])
def test_share_ended_step_is_idempotent(tmp_path, user_version):
    path = tmp_path / "shares.db"
    with closing(sqlite3.connect(path)) as db:
        db.execute("CREATE TABLE share_pending (ws TEXT PRIMARY KEY)")
        db.execute("INSERT INTO share_pending VALUES ('workspace')")
        db.execute(f"PRAGMA user_version = {user_version}")
        db.commit()
    (step,) = DEFAULT.pending("hub", 1)
    assert step.writes == ("shares.db",)
    with pytest.raises(OrchError, match="share_ended"):
        step.verify(tmp_path)
    ctx = StepContext(tmp_path, step.writes)
    step.apply(ctx)
    step.verify(tmp_path)
    with closing(sqlite3.connect(path)) as db:
        db.execute("INSERT INTO share_ended VALUES ('reader', 'ws', 'item', 'key', 'title', 'owner', 123)")
        db.commit()
    step.apply(ctx)
    step.verify(tmp_path)
    with closing(sqlite3.connect(path)) as db:
        assert db.execute("PRAGMA user_version").fetchone() == (user_version,)
        assert db.execute("SELECT * FROM share_pending").fetchall() == [("workspace",)]
        assert db.execute("SELECT * FROM share_ended").fetchall() == [
            ("reader", "ws", "item", "key", "title", "owner", 123)
        ]
        assert [(r[1], r[2], r[3], r[5]) for r in db.execute("PRAGMA table_info(share_ended)")] == [
            ("reader", "TEXT", 1, 1),
            ("ws", "TEXT", 1, 2),
            ("item_id", "TEXT", 1, 3),
            ("key", "TEXT", 1, 0),
            ("title", "TEXT", 1, 0),
            ("by_user", "TEXT", 1, 0),
            ("at_ms", "INTEGER", 1, 0),
        ]


def test_share_ended_step_without_shares_db(tmp_path):
    write_format(tmp_path, "hub", 1)
    (step,) = DEFAULT.pending("hub", 1)
    ctx = StepContext(tmp_path, step.writes)
    step.apply(ctx)
    step.apply(ctx)
    step.verify(tmp_path)
    assert apply(tmp_path, "hub") == [step.name]
    assert read_format(tmp_path, "hub") == 2
    assert apply(tmp_path, "hub") == []
    assert not (tmp_path / "shares.db").exists()
