"""Focus phase 2, P2-1: ``run.transcript`` (cap ``focus/1``): host checks, the runner command, column P only."""

# ruff: noqa: F811, E501

from __future__ import annotations

import shutil
import sys
from pathlib import Path

import pytest

from orch.host.runners import new_run_id
from orch.runner import transcript
from tests.host.dispatch_helpers import err, ok
from tests.host.test_runner_socket import GEN1, fields
from tests.host.test_terms import open_shell, ready, rstate, w  # noqa: F401  (fixtures)

pytestmark = pytest.mark.skipif(sys.platform not in ("darwin", "linux"), reason="the runner is POSIX only")
FIX = Path(__file__).parent.parent / "runner" / "fixtures" / "transcripts"


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    home = tmp_path / "claude-config"
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(home))
    return home


def as_claude(w, run, cwd="/work/proj"):
    w.h.host.runners.store.records[run]["harness"] = "claude-code"
    w.runner.sup.table.update(run, harness="claude-code", cwd_path=cwd)


def test_a_shell_run_has_no_transcript(ready):
    w = ready
    c = w.conn()
    run = open_shell(c)["run"]
    data = ok(c.call("run.transcript", {"run": run}))["data"]
    assert data["available"] is False and data["items"] == [] and data["next"] is None


def test_the_runner_reads_the_claude_transcript_by_cursor(cfg, ready):
    w = ready
    c = w.conn()
    run = open_shell(c)["run"]
    as_claude(w, run)
    first = ok(c.call("run.transcript", {"run": run}))["data"]
    assert first["available"] is False and first["reason"] == "transcript not found yet"
    proj = cfg / "projects" / "-work-proj"
    proj.mkdir(parents=True)
    shutil.copy(FIX / "claude_basic.jsonl", proj / f"{transcript.session_id(run)}.jsonl")
    page = ok(c.call("run.transcript", {"run": run, "limit": 3}))["data"]
    assert page["available"] is True and page["harness"] == "claude-code" and 3 <= len(page["items"]) < 8
    assert page["items"][0]["text"].startswith("Rate-limit") and page["eof"] is False
    rest = ok(c.call("run.transcript", {"run": run, "after": page["next"]}))["data"]
    assert rest["eof"] is True and rest["items"][0]["kind"] == "result"
    done = ok(c.call("run.transcript", {"run": run, "after": rest["next"]}))["data"]
    assert done["items"] == [] and done["eof"] is True and done["next"] == rest["next"]


def test_only_the_runs_person_reads_and_only_as_column_p(ready):
    w = ready
    c = w.conn()
    other = new_run_id()
    w.h.run(w.h.host.runners._save({**fields(w), "run": other, "person": w.quinn.ref, "workspace_id": w.wid,
                                     "state": "running", "exit": None, "cleanup": None, "runner_uid": 0,
                                     "gen": GEN1}))  # fmt: skip
    assert err(c.call("run.transcript", {"run": other})) == "not_found"
    assert err(c.call("run.transcript", {"run": new_run_id()})) == "not_found"
    mine = open_shell(c)["run"]
    assert err(c.call("run.transcript", {"run": mine}, grant=w.lab.ws.grant)) == "op.not_allowed"  # G


def test_a_run_on_a_ticket_the_person_cannot_see_is_not_found(ready):
    w = ready
    c = w.conn()
    uid, key = w.ticket("hidden")
    w.restrict(uid, [w.quinn.ref])
    run = new_run_id()
    w.h.run(w.h.host.runners._save({**fields(w), "run": run, "ticket": key, "workspace_id": w.wid, "state": "running",
                                     "exit": None, "cleanup": None, "runner_uid": 0, "gen": GEN1}))  # fmt: skip
    assert err(c.call("run.transcript", {"run": run})) == "not_found"


@pytest.mark.parametrize(
    "args",
    [
        {},
        {"run": "x"},
        {"run": "r_01J0000000000000000000000A", "after": "7"},
        {"run": "r_01J0000000000000000000000A", "limit": 0},
        {"run": "r_01J0000000000000000000000A", "child": "../x"},
        {"run": "r_01J0000000000000000000000A", "extra": 1},
    ],
)
def test_arguments_are_checked(ready, args):
    assert err(ready.conn().call("run.transcript", args)) == "invalid.input"


def test_the_op_is_listed_under_cap_focus_1():
    from orch.host.dispatch import RegistryBackend, op_kind

    assert "focus/1" in RegistryBackend().caps
    assert op_kind("run.transcript") == ("person_read", "read")


# -- P2-2: delegation.list ---------------------------------------------------------------------------------------------


def agent_on(w, run, key):
    rec = w.h.host.runners.store.records[run]
    rec.update(kind="agent", ticket=key, session="s_main", model="sonnet")
    as_claude(w, run)
    return rec


def subagent(cfg, run, aid, *, finished=True, files=("src/a.py",)):
    import json

    sub = cfg / "projects" / "-work-proj" / transcript.session_id(run) / "subagents"
    sub.mkdir(parents=True, exist_ok=True)
    meta = {"agentType": "general-purpose", "description": f"Task {aid}", "spawnDepth": 1, "model": "sonnet",
            "finished": finished}  # fmt: skip
    (sub / f"agent-{aid}.meta.json").write_text(json.dumps(meta))
    recs = [
        {"type": "assistant", "uuid": f"{aid}{n}", "timestamp": f"2026-10-26T10:0{n}:00Z", "isSidechain": True,
         "cwd": "/work/proj", "message": {"role": "assistant", "content": [
             {"type": "tool_use", "id": f"t{n}", "name": "Edit", "input": {"file_path": f"/work/proj/{f}"}}]}}
        for n, f in enumerate(files)
    ]  # fmt: skip
    (sub / f"agent-{aid}.jsonl").write_text("".join(json.dumps(r) + "\n" for r in recs))
    proj = cfg / "projects" / "-work-proj"
    parent = proj / f"{transcript.session_id(run)}.jsonl"
    if not parent.exists():
        parent.write_text("")


def test_delegation_list_joins_runner_children_and_human_children(cfg, ready):
    w = ready
    c = w.conn()
    uid, key = w.ticket("deleg")
    run = open_shell(c)["run"]
    agent_on(w, run, key)
    subagent(cfg, run, "aaa", files=("src/a.py", "src/b.py"))
    subagent(cfg, run, "bbb", finished=False)
    kid = new_run_id()
    w.h.run(w.h.host.runners._save({**fields(w), "run": kid, "kind": "agent", "ticket": key, "workspace_id": w.wid,
                                     "state": "running", "exit": None, "cleanup": None, "runner_uid": 0, "gen": GEN1,
                                     "harness": "codex", "label": "Review it", "parent_run": run,
                                     "spawned_by": "human"}))  # fmt: skip
    data = ok(c.call("delegation.list", {"ticket": key}))["data"]
    assert data["ticket"] == key and [s["run"] for s in data["sessions"]] == [run]  # the child is not its own session
    s = data["sessions"][0]
    assert s["visibility"] == {"available": True} and s["harness"] == "claude-code" and s["session"] == "s_main"
    kids = {k["id"]: k for k in s["children"]}
    assert set(kids) == {"cc:agent-aaa", "cc:agent-bbb", f"run:{kid}"}
    a = kids["cc:agent-aaa"]
    assert a["parent_run"] == run and a["state"] == "done" and a["files"] == ["src/a.py", "src/b.py"]
    assert a["transcript"] == {"run": run, "child": "agent-aaa"} and a["spawned_by"] == "agent"
    assert kids["cc:agent-bbb"]["state"] == "running"
    h = kids[f"run:{kid}"]
    assert (h["spawned_by"], h["state"], h["title"], h["run"], h["transcript"]) == (
        "human",
        "running",
        "Review it",
        kid,
        {"run": kid},
    )
    child = ok(c.call("run.transcript", {"run": run, "child": "agent-aaa"}))["data"]
    assert child["available"] is True and [i["kind"] for i in child["items"]] == ["tool", "tool"]


def test_delegation_list_other_tickets_and_other_people_are_left_out(cfg, ready):
    w = ready
    c = w.conn()
    uid, key = w.ticket("deleg-a")
    uid2, key2 = w.ticket("deleg-b")
    mine = open_shell(c)["run"]
    agent_on(w, mine, key2)
    other = new_run_id()
    w.h.run(w.h.host.runners._save({**fields(w), "run": other, "kind": "agent", "person": w.quinn.ref, "ticket": key,
                                     "workspace_id": w.wid, "state": "running", "exit": None, "cleanup": None,
                                     "runner_uid": 0, "gen": GEN1, "harness": "claude-code"}))  # fmt: skip
    assert ok(c.call("delegation.list", {"ticket": key}))["data"]["sessions"] == []
    assert [s["run"] for s in ok(c.call("delegation.list", {"ticket": key2}))["data"]["sessions"]] == [mine]


def test_delegation_list_without_a_transcript_has_no_children(cfg, ready):
    w = ready
    c = w.conn()
    uid, key = w.ticket("deleg-none")
    run = open_shell(c)["run"]
    agent_on(w, run, key)
    s = ok(c.call("delegation.list", {"ticket": key}))["data"]["sessions"][0]
    assert s["children"] == [] and s["files"] == [] and s["visibility"]["available"] is True


def test_delegation_list_checks_ticket_column_and_arguments(ready):
    w = ready
    c = w.conn()
    uid, key = w.ticket("deleg-hidden")
    w.restrict(uid, [w.quinn.ref])
    assert err(c.call("delegation.list", {"ticket": key})) == "not_found"
    assert err(c.call("delegation.list", {"ticket": "NOPE-1"})) == "not_found"
    assert err(c.call("delegation.list", {})) == "invalid.input"
    assert err(c.call("delegation.list", {"ticket": key, "x": 1})) == "invalid.input"
    assert err(c.call("delegation.list", {"ticket": key}, grant=w.lab.ws.grant)) == "op.not_allowed"  # G


def test_delegation_list_is_a_column_p_read_under_cap_focus_1():
    from orch.host.dispatch import op_kind
    from orch.host.transcripts import TRANSCRIPT_OPS

    assert TRANSCRIPT_OPS["delegation.list"] == 1 and op_kind("delegation.list") == ("person_read", "read")
