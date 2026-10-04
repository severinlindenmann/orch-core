import os
import time

import pytest

from orch.core import maintenance, query, store
from orch.errors import UsageError


def test_list_filters(ws, put):
    a = put("open", labels=["infra"])
    b = put("backlog")
    assert [e.id for e in query.list_tickets(ws)] == [b, a]  # status order, backlog first
    assert [e.id for e in query.list_tickets(ws, status="open")] == [a]
    assert [e.id for e in query.list_tickets(ws, label="infra")] == [a]
    with pytest.raises(UsageError):
        query.list_tickets(ws, status="nope")


def test_list_mine(ws, aops, put):
    tid = put("open")
    aops.claim(tid)
    put("open")
    assert [e.id for e in query.list_tickets(ws, session="7f3c9a21-0000")] == [tid]


def test_next_orders_by_priority_and_skips_blocked(ws, put):
    low = put("open", priority="low")
    urgent = put("open", priority="urgent")
    blocker = put("in-progress")
    put("open", priority="urgent", blocked_by=[blocker])
    done = put("done")
    unblocked = put("open", priority="high", blocked_by=[done])
    assert [e.id for e in query.next_tickets(ws)] == [urgent, unblocked, low]


def test_search(ws, put):
    a = put("backlog", sections={"Ask": "nightly export"})
    put("backlog")
    assert [e.id for e in query.search(ws, "nightly")] == [a]


def test_ticket_view(ws, aops, put, tmp_path):
    tid = put("open")
    f = tmp_path / "a.png"
    f.write_bytes(b"x")
    aops.artifact_add(tid, f)
    path, t = store.load(ws, tid)
    v = query.ticket_view(ws, path, t)
    assert v["path"].startswith("tickets/open/")
    assert v["gates"] == {"requirements": "pending", "plan": "pending"}
    assert v["artifacts"] == [f"{tid}/a.png"]


def test_build_index(ws, put):
    tid = put("open", title="Pipe | in title")
    text = maintenance.build_index(ws).read_text(encoding="utf-8")
    assert "## open (1)" in text and f"[{tid}](open/" in text
    assert "Pipe \\| in title" in text and "## done (0)" in text


def test_tidy(ws):
    old = ws.temporary_dir / "sub" / "old.log"
    old.parent.mkdir(parents=True)
    old.write_text("x", encoding="utf-8")
    new = ws.temporary_dir / "new.log"
    new.write_text("y", encoding="utf-8")
    past = time.time() - 30 * 86400
    os.utime(old, (past, past))
    removed = maintenance.tidy(ws)
    assert removed == [old] and new.exists() and not old.parent.exists()
