"""Focus phase 2, P2-2: the runner's delegation reader on hand-written synthetic transcripts."""

# ruff: noqa: E501

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from orch.runner import transcript, transcript_children
from orch.runner.supervise import SpawnError

CWD = "/work/proj"
RUN = "r_01J0000000000000000000000A"


def jl(path: Path, *recs: dict) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r) + "\n" for r in recs))
    return path


def edit(uid: str, file: str, ts: str) -> dict:
    return {"type": "assistant", "uuid": uid, "timestamp": ts, "isSidechain": True, "cwd": CWD,
            "message": {"role": "assistant", "content": [{"type": "tool_use", "id": "t" + uid, "name": "Edit",
                                                          "input": {"file_path": f"{CWD}/{file}"}}]}}  # fmt: skip


def claude_parent(tmp_path: Path) -> Path:
    parent = jl(
        tmp_path / "projects" / "-work-proj" / f"{transcript.session_id(RUN)}.jsonl",
        {"type": "assistant", "uuid": "p1", "timestamp": "2026-10-26T10:00:00Z", "cwd": CWD,
         "message": {"role": "assistant", "content": [{"type": "tool_use", "id": "tA", "name": "Edit",
                                                       "input": {"file_path": f"{CWD}/src/main.py"}}]}},
        {"type": "user", "uuid": "p2", "timestamp": "2026-10-26T10:05:00Z", "toolUseResult": {"agentId": "aaa", "status": "completed"},
         "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "tA", "content": "ok"}]}},
        {"type": "user", "uuid": "p3", "timestamp": "2026-10-26T10:06:00Z", "toolUseResult": {"agentId": "bbb", "status": "failed"},
         "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "tB", "content": "no"}]}},
    )  # fmt: skip
    sub = parent.with_suffix("") / "subagents"
    for aid, desc, fin in (("aaa", "Write tests", True), ("bbb", "Broken task", True), ("ccc", "Still going", False)):
        meta = {"agentType": "general-purpose", "description": desc, "toolUseId": "t" + aid, "spawnDepth": 1,
                "model": "sonnet", "finished": fin}  # fmt: skip
        sub.mkdir(parents=True, exist_ok=True)
        (sub / f"agent-{aid}.meta.json").write_text(json.dumps(meta))
    jl(
        sub / "agent-aaa.jsonl",
        edit("a1", "tests/test_x.py", "2026-10-26T10:01:00Z"),
        edit("a2", "tests/test_y.py", "2026-10-26T10:04:00Z"),
    )
    jl(sub / "agent-bbb.jsonl", edit("b1", "src/b.py", "2026-10-26T10:02:00Z"))
    return parent


def test_delegation_claude_children_come_from_the_subagents_folder(tmp_path):
    kids = {k["id"]: k for k in transcript_children.children(claude_parent(tmp_path), "claude-code", CWD)}
    assert set(kids) == {"cc:agent-aaa", "cc:agent-bbb", "cc:agent-ccc"}
    a = kids["cc:agent-aaa"]
    assert (a["title"], a["state"], a["spawned_by"], a["model"], a["depth"]) == (
        "Write tests",
        "done",
        "agent",
        "sonnet",
        1,
    )
    assert a["files"] == ["tests/test_x.py", "tests/test_y.py"] and a["transcript"] == {"child": "agent-aaa"}
    assert a["started_at"] == "2026-10-26T10:01:00Z" and a["ended_at"] == "2026-10-26T10:04:00Z"
    assert kids["cc:agent-bbb"]["state"] == "failed"
    c = kids["cc:agent-ccc"]
    assert c["state"] == "running" and c["ended_at"] is None and c["files"] == []


def test_delegation_without_a_subagents_folder_has_no_children(tmp_path):
    p = jl(tmp_path / "x.jsonl", {"type": "system", "subtype": "turn_duration"})
    assert transcript_children.children(p, "claude-code", CWD) == []
    assert transcript_children.children(p, "shell", CWD) == []


def test_delegation_a_broken_meta_file_is_skipped(tmp_path):
    parent = claude_parent(tmp_path)
    (parent.with_suffix("") / "subagents" / "agent-aaa.meta.json").write_text("{not json")
    ids = {k["id"] for k in transcript_children.children(parent, "claude-code", CWD)}
    assert ids == {"cc:agent-bbb", "cc:agent-ccc"}


def codex_tree(tmp_path: Path) -> tuple[Path, Path]:
    day = tmp_path / "codex" / "sessions" / "2026" / "10" / "26"
    pid, kid = "01a1-parent", "01a1-child"
    parent = jl(day / f"rollout-2026-10-26T10-00-00-{pid}.jsonl",
                {"timestamp": "2026-10-26T10:00:00Z", "type": "session_meta", "payload": {"id": pid, "cwd": CWD, "source": "exec", "timestamp": "2026-10-26T10:00:00Z"}})  # fmt: skip
    child = jl(
        day / f"rollout-2026-10-26T10-01-00-{kid}.jsonl",
        {"timestamp": "2026-10-26T10:01:00Z", "type": "session_meta",
         "payload": {"id": kid, "cwd": CWD, "timestamp": "2026-10-26T10:01:00Z", "source": {"subagent": {"thread_spawn": {
             "parent_thread_id": pid, "depth": 1, "agent_path": "/root/say_hi", "agent_nickname": "Heisenberg"}}}}},
        {"timestamp": "2026-10-26T10:02:00Z", "type": "response_item",
         "payload": {"type": "custom_tool_call", "call_id": "c1", "name": "apply_patch",
                     "input": "*** Begin Patch\n*** Update File: src/hi.py\n*** End Patch"}},
        {"timestamp": "2026-10-26T10:03:00Z", "type": "event_msg", "payload": {"type": "task_complete"}},
    )  # fmt: skip
    jl(day / "rollout-2026-10-26T10-02-00-01a1-other.jsonl",  # a guardian rollout and an unrelated one: never children
       {"timestamp": "2026-10-26T10:02:00Z", "type": "session_meta", "payload": {"id": "01a1-other", "cwd": CWD, "source": {"subagent": {"other": "guardian"}}}})  # fmt: skip
    return parent, child


def test_delegation_codex_children_are_found_by_the_parent_thread_link(tmp_path):
    parent, child = codex_tree(tmp_path)
    env = {"CODEX_HOME": str(tmp_path / "codex")}
    kids = transcript_children.children(parent, "codex", CWD, env)
    assert [k["id"] for k in kids] == ["cx:agent-01a1-child"]
    k = kids[0]
    assert (k["title"], k["state"], k["depth"], k["files"]) == ("say_hi", "done", 1, ["src/hi.py"])
    assert transcript_children.codex_child_path(parent, "agent-01a1-child", env) == child
    assert transcript_children.codex_child_path(parent, "agent-01a1-other", env) is None


class Table(dict):
    def update(self, run, **kw):  # pragma: no cover - not used
        self[run].update(kw)


def test_delegation_serve_returns_own_files_and_children(tmp_path, monkeypatch):
    claude_parent(tmp_path)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
    sup = SimpleNamespace(table=Table({RUN: {"harness": "claude-code", "cwd_path": CWD}}))
    env = {"CLAUDE_CONFIG_DIR": str(tmp_path)}
    got = transcript_children.serve({"run": RUN, "children": True}, sup, env)
    assert got["available"] is True and got["files"] == ["src/main.py"] and len(got["children"]) == 3
    none = transcript_children.serve(
        {"run": RUN, "children": True},
        SimpleNamespace(table=Table({RUN: {"harness": "claude-code", "cwd_path": CWD}})),
        {"CLAUDE_CONFIG_DIR": str(tmp_path / "nope")},
    )
    assert none["children"] == [] and none["files"] == []
    with pytest.raises(SpawnError):
        transcript_children.serve({"run": "r_missing", "children": True}, sup, env)


def test_delegation_child_transcript_reads_the_codex_rollout(tmp_path):
    parent, _ = codex_tree(tmp_path)
    sup = SimpleNamespace(
        table=Table({RUN: {"harness": "codex", "cwd_path": CWD, "created_at": "2026-10-26T09:59:00Z"}})
    )
    env = {"CODEX_HOME": str(tmp_path / "codex")}
    got = transcript.serve({"run": RUN, "child": "agent-01a1-child"}, sup, env)
    assert got["available"] is True and [i["kind"] for i in got["items"]] == ["tool", "status"]
