"""Focus phase 2, P2-1: the runner's transcript reader on hand-written synthetic fixtures."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from orch.runner import transcript
from orch.runner.supervise import SpawnError

FIX = Path(__file__).parent / "fixtures" / "transcripts"
CWD = "/work/proj"
RUN = "r_01J0000000000000000000000A"


def lines(name: str) -> list[dict]:
    return [json.loads(x) for x in (FIX / name).read_text().splitlines()]


def test_session_id_is_deterministic_and_the_placeholder_is_replaced_once():
    sid = transcript.session_id(RUN)
    assert sid == transcript.session_id(RUN) != transcript.session_id(RUN[:-1] + "B")
    argv = ["a", "--model", "m", "--session-id", "@orch-session-id", "prompt"]
    assert transcript.prepare(RUN, argv) == ["a", "--model", "m", "--session-id", sid, "prompt"]
    assert transcript.prepare(RUN, ["a"]) == ["a"]
    for bad in (["a", "@orch-session-id"], ["--session-id", "@orch-session-id", "@orch-session-id"]):
        with pytest.raises(SpawnError):
            transcript.prepare(RUN, bad)


def test_claude_records_are_normalized():
    items = []
    for rec in lines("claude_basic.jsonl"):
        items += transcript.normalize_claude(rec, CWD)
    by = {i["id"]: i for i in items}
    assert [i["kind"] for i in items] == ["text", "text", "tool", "tool", "result", "result", "question", "status"]
    assert by["u1"]["role"] == "user" and by["u1"]["text"].startswith("Rate-limit")
    assert by["a2:0"]["text"] == "I will edit it."
    edit = by["a2:1"]
    assert edit["text"] == f"Edit {CWD}/src/api/invite.py" and edit["files"] == ["src/api/invite.py"]
    assert edit["tool"] == {"name": "Edit", "call": "toolu_01"}
    assert "files" not in by["a2:2"]  # outside the cwd: dropped
    assert by["toolu_01:r"]["error"] is False and by["toolu_02:r"]["error"] is True
    assert by["toolu_02:r"]["text"] == "denied"
    assert by["a3"]["options"] == ["Per workspace", "Per user"] and by["a3"]["text"].startswith("Where")
    assert by["s1"]["text"] == "turn complete"
    assert not any(i["text"] == "sidechain text" for i in items)
    side = transcript.normalize_claude(lines("claude_basic.jsonl")[8], CWD, sidechain=True)
    assert [i["text"] for i in side] == ["sidechain text"]


def test_codex_records_are_normalized():
    items = []
    for n, rec in enumerate(lines("codex_basic.jsonl")):
        items += transcript.normalize_codex(rec, CWD, key=str(n))
    kinds = [(i["role"], i["kind"]) for i in items]
    assert kinds == [
        ("user", "text"),
        ("assistant", "text"),
        ("assistant", "tool"),
        ("tool", "result"),
        ("assistant", "tool"),
        ("assistant", "tool"),
        ("system", "status"),
    ]
    assert items[0]["text"] == "Add a test."
    patch = items[2]
    assert patch["files"] == ["src/a.py", "tests/test_a.py"] and patch["tool"]["call"] == "call_1"
    assert items[3]["text"] == "Success" and items[3]["id"] == "call_1:r"
    assert items[4]["text"] == "shell ls -la"
    assert items[5]["tool"]["name"] == "web_search" and "redis" in items[5]["text"]


def test_text_is_capped():
    rec = {"type": "assistant", "uuid": "x", "message": {"content": [{"type": "text", "text": "é" * 5000}]}}
    (item,) = transcript.normalize_claude(rec, CWD)
    assert item["truncated"] is True and len(item["text"].encode()) <= transcript.MAX_TEXT


def test_read_is_cursor_based_and_stops_at_a_partial_line(tmp_path):
    src = (FIX / "claude_basic.jsonl").read_bytes()
    path = tmp_path / "t.jsonl"
    cut = src.rindex(b'{"type": "api-request-x"')
    path.write_bytes(src[: cut + 10])  # the last line is partial
    first = transcript.read(path, "claude-code", 0, cwd=CWD)
    assert first["eof"] is False and first["next"] == f"o:{cut}"
    assert len(first["items"]) == 8
    path.write_bytes(src)
    second = transcript.read(path, "claude-code", int(first["next"][2:]), cwd=CWD)
    assert second["items"] == [] and second["eof"] is True and second["next"] == f"o:{len(src)}"
    again = transcript.read(path, "claude-code", len(src), cwd=CWD)
    assert again["items"] == [] and again["eof"] is True


def test_read_limit_pages_without_losing_items(tmp_path):
    path = tmp_path / "t.jsonl"
    shutil.copy(FIX / "claude_basic.jsonl", path)
    got, after = [], 0
    for _ in range(20):
        page = transcript.read(path, "claude-code", after, 3, cwd=CWD)
        got += page["items"]
        after = int(page["next"][2:])
        if page["eof"]:
            break
    whole = transcript.read(path, "claude-code", 0, cwd=CWD)
    assert [i["id"] for i in got] == [i["id"] for i in whole["items"]]


def test_unparseable_and_overlong_lines_are_skipped(tmp_path, monkeypatch):
    path = tmp_path / "t.jsonl"
    ok = json.dumps({"type": "user", "uuid": "k", "message": {"content": "hi"}})
    path.write_bytes(b"not json\n" + ok.encode() + b"\n")
    got = transcript.read(path, "claude-code", 0)
    assert [i["id"] for i in got["items"]] == ["k"] and got["skipped"] == 1
    monkeypatch.setattr(transcript, "MAX_READ", 16)
    path.write_bytes(b"x" * 40 + b"\n" + ok.encode() + b"\n")
    cursor = 0
    for _ in range(10):
        page = transcript.read(path, "claude-code", cursor)
        cursor = int(page["next"][2:])
        if page["items"]:
            break
    assert cursor > 0  # it moved past the long line instead of stalling


class FakeSup:
    def __init__(self, rec):
        self.table = type("T", (), {"get": staticmethod(lambda run: rec if run == RUN else None)})()


def test_claude_locate_and_serve(tmp_path):
    home = tmp_path / "claude"
    proj = home / "projects" / "-work-proj"
    proj.mkdir(parents=True)
    path = proj / f"{transcript.session_id(RUN)}.jsonl"
    shutil.copy(FIX / "claude_basic.jsonl", path)
    sub = path.with_suffix("") / "subagents"
    sub.mkdir(parents=True)
    (sub / "agent-a04b.jsonl").write_text(
        json.dumps({"type": "assistant", "isSidechain": True, "uuid": "c1", "message": {"content": "child says hi"}})
        + "\n"
    )
    env = {"CLAUDE_CONFIG_DIR": str(home), "HOME": str(tmp_path)}
    sup = FakeSup({"harness": "claude-code", "cwd_path": CWD, "created_at": "2026-10-26T10:00:00.000Z"})
    out = transcript.serve({"run": RUN}, sup, env)
    assert out["available"] is True and out["harness"] == "claude-code" and len(out["items"]) == 8
    child = transcript.serve({"run": RUN, "child": "agent-a04b"}, sup, env)
    assert [i["text"] for i in child["items"]] == ["child says hi"]
    for bad in ({"child": "../x"}, {"after": "7"}):
        with pytest.raises(SpawnError):
            transcript.serve({"run": RUN, **bad}, sup, env)
    with pytest.raises(SpawnError):
        transcript.serve({"run": "r_nope"}, sup, env)
    missing = transcript.serve({"run": RUN}, sup, {"CLAUDE_CONFIG_DIR": str(tmp_path / "none")})
    assert missing["available"] is False and missing["items"] == [] and missing["next"] is None


def test_codex_locate_picks_by_cwd_and_start_time(tmp_path):
    home = tmp_path / "codex"
    day = home / "sessions" / "2026" / "10" / "26"
    day.mkdir(parents=True)

    def rollout(name: str, cwd: str, ts: str) -> Path:
        p = day / name
        p.write_text(
            json.dumps({"timestamp": ts, "type": "session_meta", "payload": {"cwd": cwd, "timestamp": ts}}) + "\n"
        )
        return p

    rollout("rollout-1-other.jsonl", "/elsewhere", "2026-10-26T10:40:00.000Z")
    rollout("rollout-2-early.jsonl", CWD, "2026-10-26T09:00:00.000Z")
    mine = rollout("rollout-3-mine.jsonl", CWD, "2026-10-26T10:40:00.000Z")
    # mtime is "now" for all of them; the early one is rejected by its own session_meta time
    rec = {"harness": "codex", "cwd_path": CWD, "created_at": "2026-10-26T10:39:58.000Z"}
    import os

    for p in day.iterdir():
        os.utime(p, (4_000_000_000, 4_000_000_000))
    env = {"CODEX_HOME": str(home), "HOME": str(tmp_path)}
    transcript._CODEX_CACHE.clear()
    assert transcript.locate(RUN, "codex", rec, env) == mine
    mine.unlink()
    assert transcript.locate(RUN, "codex", rec, env) is None  # a stale cache entry is not trusted
    assert transcript.locate(RUN, "codex", {**rec, "cwd_path": None}, env) is None
    assert transcript.locate(RUN, "shell", rec, env) is None


def test_harness_of_follows_runner_json_for_a_program_with_another_name():
    from orch.runner.config import parse
    from orch.runner.link import _harness_of

    cfg = parse({"v": 1, "login_shell": False, "programs": {"claude-code": "/opt/wrap/my-agent", "codex": "/opt/wrap/cx"}})
    assert _harness_of(["/opt/wrap/my-agent", "--session-id", "x"], cfg) == "claude-code"
    assert _harness_of(["/opt/wrap/cx"], cfg) == "codex"
    assert _harness_of(["/opt/wrap/my-agent"]) is None  # without the config only the program's name counts
    assert _harness_of(["/usr/bin/claude"], cfg) == "claude-code"
