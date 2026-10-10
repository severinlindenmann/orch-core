"""The last review round of #351: hidden git flags, refs without objects, damaged notes, observe outside the lock, an
invalidation that wait hears, hints by status, the wording of ``+N more``."""

from __future__ import annotations

import json

from tests.ops.helpers import wait_for
from tests.ops.test_observe import LINKS, types
from tests.ops.test_task_ac import claimed, fill_and_approve, py


def test_skip_worktree_and_assume_unchanged_files_count_as_a_dirty_tree(ws, cli):
    repo = ws.repo()
    claimed(cli)
    cli("task", "add", "t", "--verify", py("print(1)"))
    cli("task", "add", "u", "--verify", py("print(2)"))
    cli("set", "1", LINKS)
    ws.git("update-index", "--skip-worktree", "impl.txt")
    (repo / "impl.txt").write_text("secretly changed\n")
    r = cli("task", "done", "T1", "--run")
    assert r.code == 0 and "looks uncommitted" in r.out and ws.events("1")[-1]["receipt"]["commit"] is None
    ws.git("update-index", "--no-skip-worktree", "impl.txt")
    ws.git("checkout", "impl.txt")
    ws.git("update-index", "--assume-unchanged", "impl.txt")
    r = cli("task", "done", "T2", "--run")
    assert "looks uncommitted" in r.out and ws.events("1")[-1]["receipt"]["commit"] is None


def test_a_ref_that_names_no_object_is_reported_not_signed(ws, cli):
    repo = ws.repo()
    claimed(cli)
    cli("set", "1", LINKS)
    (repo / ".git" / "refs" / "heads" / "feat").mkdir(parents=True, exist_ok=True)
    (repo / ".git" / "refs" / "heads" / "feat" / "x").write_text("ab" * 20 + "\n")
    r = cli("show")
    assert "observe: proj: refs/heads/feat/x names no commit object; nothing was recorded" in r.out
    assert "branch.pushed" not in types(ws)


def test_malformed_notes_never_crash_and_are_reported(ws, cli):
    claimed(cli)
    p = next((ws.root / ".state" / "sessions").glob("*.notes.json"))
    doc = json.loads(p.read_text())
    uid = next(iter(doc["tickets"]))
    for bad in ({"decided": "x"}, {"cursor": [1]}, {"base": "no"}, {"decided": -4, "cursor": True}):
        doc["tickets"][uid] = bad
        p.write_text(json.dumps(doc))
        r = cli("show")
        assert r.code == 0 and "session notes were damaged" in r.out, bad
        assert cli("wait", "--timeout", "1").code == 0, bad
    p.write_text(json.dumps({"tickets": [], "claims": "x"}))
    assert cli.j("status").code == 0


def test_notes_keep_claims_and_unread_decisions_past_the_limit(ws, cli):
    from orch.ops.runtime import Notes

    n = Notes(ws.root / ".state" / "sessions", "s_01J9ZP0000000000000000000S")
    n.add_claim("CLAIMED")
    n.update("CLAIMED", now=1)
    n.update("UNREAD", now=2, keep=True)
    for i in range(150):
        n.update(f"T{i}", now=10 + i)
    doc = json.loads(next((ws.root / ".state" / "sessions").glob("*.notes.json")).read_text())
    assert "CLAIMED" in doc["tickets"] and "UNREAD" in doc["tickets"] and len(doc["tickets"]) <= Notes.KEEP
    n.update("UNREAD", now=3, decided=5, keep=False)
    for i in range(150):
        n.update(f"U{i}", now=500 + i)
    assert (
        "UNREAD" not in json.loads(next((ws.root / ".state" / "sessions").glob("*.notes.json")).read_text())["tickets"]
    )


def test_submit_reads_git_before_it_takes_the_lock(ws, cli, monkeypatch):
    from orch.ops import plans
    from orch.store import Store, observe

    ws.repo()
    claimed(cli)
    cli("set", "1", LINKS)
    order = []
    real_git = observe.git
    monkeypatch.setattr(observe, "git", lambda *a, **k: order.append("git") or real_git(*a, **k))
    real_locked = Store.locked
    from contextlib import contextmanager

    @contextmanager
    def locked(self):
        order.append("lock")
        with real_locked(self):
            yield

    monkeypatch.setattr(Store, "locked", locked)
    cli("submit")
    assert "git" in order and order.index("lock") > max(i for i, x in enumerate(order) if x == "git")
    assert plans.run  # the frame observes first, then locks


def test_new_commits_void_the_verify_approval_and_wait_hears_it(ws, cli):
    from tests.ops.test_ask_wait import OPEN, verdict

    repo = ws.repo()
    claimed(cli)
    fill_and_approve(ws, cli)
    cli("set", "1", LINKS)
    cli("section", "set", "verification", "-m", "run it")
    cli("task", "done", "T1", "--run")
    ws.store.append(ws.person_event(ws.owner, "workspace", "policy.changed", gates={"verify": OPEN}), log="workspace")
    assert cli("submit").code == 0
    uid = ws.uid("1")
    ws.store = ws.other()
    verdict(ws, uid, "pass")
    assert wait_for(cli, "verdict", ref="1").data["outcome"] == "pass"
    (repo / "impl.txt").write_text("new work\n")
    ws.git("commit", "-qam", "more")
    assert cli("show", "1").code == 0  # observes the new head (the ticket is done: no claim to default to)
    ev = [e for e in ws.events("1") if e["type"] in ("branch.pushed", "gate.invalidated")]
    assert [e["type"] for e in ev] == ["branch.pushed", "branch.pushed", "gate.invalidated"]
    assert ev[-1]["gate"] == "verify" and ev[-1]["cause"] == "new_commits" and len(ev[-1]["voided"]) == 1
    r = wait_for(cli, "invalidated", ref="1")
    assert r.code == 3 and r.data["gate"] == "verify"


def test_next_hint_respects_the_status(ws, cli):
    from tests.ops.test_ask_wait import make_ready

    make_ready(ws, cli)  # submitted: testing
    r = cli("log", "waiting now")
    assert r.out.splitlines()[-1] == "next: orch wait"
    assert cli("show").out.splitlines()[-1] == "next: orch wait"


def test_more_is_exact_or_says_how_far_it_looked(ws, cli, monkeypatch):
    for i in range(4):
        cli("new", f"T{i}")
    assert "+3 more (" in cli("list", "--limit", "1").out
    import orch.ops.commands.list as lst

    monkeypatch.setattr(lst, "MORE_SCAN", 1)
    out = cli("list", "--limit", "1").out
    assert "1 or more not shown (" in out and "+1+" not in out and "+" + "2+" not in out
