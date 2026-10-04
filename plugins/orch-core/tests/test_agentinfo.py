"""What the agent in a Terminals session is doing (issue #40): read from Claude Code's own files, never sent anywhere."""
import json
import os
import time
from types import SimpleNamespace

import pytest

from orch.dashboard import agentinfo, terminals
from test_terminals import dash  # noqa: F401  (the local dashboard: Terminals answer only to it)

SID = "11111111-2222-3333-4444-555555555555"
PID = 4000


def _write(path, lines):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        for d in lines:
            f.write(json.dumps(d) + "\n")


def _assistant(content, model="claude-opus-5-5", out=10, ctx=(1, 100, 50)):
    return {"type": "assistant", "message": {"model": model, "content": content, "usage": {
        "input_tokens": ctx[0], "cache_read_input_tokens": ctx[1], "cache_creation_input_tokens": ctx[2],
        "output_tokens": out}}}


@pytest.fixture
def claude(tmp_path, monkeypatch):
    """A fake Claude Code folder: the session file of pid 4000 and its transcript."""
    root = tmp_path / "claude"
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(root))
    agentinfo._TRANSCRIPTS.clear()
    agentinfo._SUB_TRANSCRIPTS.clear()
    agentinfo._TICKETS.clear()
    (root / "sessions").mkdir(parents=True)
    (root / "sessions" / f"{PID}.json").write_text(json.dumps({"pid": PID, "sessionId": SID, "status": "busy",
                                                                "version": "2.1.288"}))
    log = root / "projects" / "-tmp-ws" / f"{SID}.jsonl"
    _write(log, [
        {"type": "ai-title", "aiTitle": "School group trips implementation"},
        {"type": "user", "message": {"content": "build L-0007 please"}},
        _assistant([{"type": "tool_use", "id": "t1", "name": "Bash",
                     "input": {"command": "npm test", "description": "Run the plan tests"}}], out=40),
        {"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "t1", "content": "ok"}]}},
        {"type": "pr-link", "prNumber": 258, "prUrl": "https://github.com/o/r/pull/258", "prRepository": "o/r"},
        {"type": "pr-link", "prNumber": 9, "prUrl": "javascript:alert(1)", "prRepository": "o/r"},
        {"type": "pr-link", "prNumber": 10, "prUrl": "https://x.example/a/T-1/f.png", "prRepository": "o/r"},
        {"type": "pr-link", "prNumber": "11 <b>", "prUrl": "https://github.com/o/r/pull/11", "prRepository": "o/r"},
        {"type": "cost-state", "totalCostUSD": 1.5, "totalLinesAdded": 12, "totalLinesRemoved": 3},
    ])
    sub = log.parent / SID / "subagents"
    sub.mkdir(parents=True)
    (sub / "agent-a.meta.json").write_text(json.dumps({"agentType": "general-purpose", "description": "Review L-0008",
                                                       "model": "sonnet"}))
    (sub / "agent-a.jsonl").write_text("{}\n")
    (sub / "agent-b.meta.json").write_text(json.dumps({"agentType": "general-purpose", "description": "Old build"}))
    (sub / "agent-b.jsonl").write_text("{}\n")
    old = time.time() - 3600
    os.utime(sub / "agent-b.jsonl", (old, old))
    return SimpleNamespace(root=root, log=log, session=SimpleNamespace(pid=PID, activity=int(time.time())))


def test_info_reads_title_step_model_tokens_prs_cost_and_subagents(ws, claude):
    i = agentinfo.info(ws, claude.session)
    assert i["known"] and i["status"] == "busy" and i["title"] == "School group trips implementation"
    assert i["now"] == "Bash · Run the plan tests" and i["now_kind"] == "step"
    assert i["model"] == "Opus 5.5" and i["context"] == 151 and i["tokens"]["output"] == 40
    assert i["last_prompt"] == "build L-0007 please"
    # never a javascript: link, never one R24's canonical_link calls inert, never a number that is not one
    assert [p["url"] for p in i["prs"]] == ["", "", "https://github.com/o/r/pull/258"]
    assert i["cost"] == {"usd": 1.5, "added": 12, "removed": 3}
    assert [(s["description"], s["active"]) for s in i["subagents"]] == [("Review L-0008", True), ("Old build", False)]
    assert i["subagents_active"] == 1


def test_a_waiting_session_shows_its_question_and_new_lines_are_read_incrementally(ws, claude):
    agentinfo.info(ws, claude.session)
    first_offset = agentinfo._TRANSCRIPTS[SID].offset
    _write(claude.log, [_assistant([{"type": "tool_use", "id": "t2", "name": "AskUserQuestion", "input": {
        "questions": [{"question": "Which layout should the wiki page use?"}]}}], model="claude-sonnet-5-5")])
    (claude.root / "sessions" / f"{PID}.json").write_text(json.dumps({"sessionId": SID, "status": "waiting"}))
    i = agentinfo.info(ws, claude.session)
    assert agentinfo._TRANSCRIPTS[SID].offset > first_offset
    assert i["now"] == "Asks: Which layout should the wiki page use?" and i["now_kind"] == "question"
    assert i["model"] == "Sonnet 5.5" and dict(i["models"]) == {"Opus 5.5": 1, "Sonnet 5.5": 1}


def test_tickets_claimed_by_the_session(ws, put, claude):
    tid = put("in-progress", title="Parents cannot turn the email off", claim={"session": SID, "harness": "claude-code"})
    put("in-progress", claim={"session": "someone-else"})
    i = agentinfo.info(ws, claude.session)
    assert [t["id"] for t in i["tickets"]] == [tid] and tid in i["sig"]


def test_no_claude_files_means_empty_fields_not_an_error(ws, tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "none"))
    i = agentinfo.info(ws, SimpleNamespace(pid=999, activity=0))
    assert not i["known"] and i["now"] == "" and i["tickets"] == [] and i["tokens"] == {}


def test_compact_and_model_label():
    assert [agentinfo.compact(n) for n in (None, 999, 71772, 188266056)] == ["—", "999", "72k", "188.3M"]
    assert agentinfo.model_label("claude-opus-5-5") == "Opus 5.5" and agentinfo.model_label("weird") == "weird"


# ---- on the pages --------------------------------------------------------------------------------------------------

def _fake_tmux(monkeypatch, ws):
    from test_terminals import FakeTmux
    t = FakeTmux(sessions=[("DEMO-1", str(ws.root))], screen="line one\nline two\n\n")
    monkeypatch.setattr(terminals, "tmux", t)
    monkeypatch.setattr(terminals, "which", lambda name: "/usr/bin/tmux")
    monkeypatch.setattr(terminals, "addon_on", lambda ws: True)
    return t


def test_the_overview_tile_says_what_the_agent_does(dash, ws, claude, monkeypatch):
    _fake_tmux(monkeypatch, ws)  # its session's pid is 4000, the fake Claude session
    html = dash.get("/terminals").text
    assert "School group trips implementation" in html and "Bash · Run the plan tests" in html
    assert 'data-tail>line one\nline two</pre>' in html  # the last lines that say something, blank rows dropped
    assert 'data-next' in html and 'data-filter="changed"' in html


def test_the_terminal_page_has_details_and_a_read_only_watch(dash, ws, claude, monkeypatch):
    _fake_tmux(monkeypatch, ws)
    html = dash.get("/terminals/DEMO-1").text
    assert "Bash · Run the plan tests" in html and 'data-mode="watch"' in html and "term-watch-note" in html
    assert "Review L-0008" in html and "1 finished" in html and "Diagnostics" in html
    assert 'href="https://github.com/o/r/pull/258"' in html and "javascript:" not in html
    assert "$1.50" in html and "not what you pay" in html


def test_cache_warm_then_cold_and_ttl_from_the_write(tmp_path):
    """A reply restarts the cache clock; a 1h write keeps it warm an hour, a 5m one five minutes; none before a reply."""
    log = tmp_path / "t.jsonl"
    t = agentinfo.Transcript(log)
    _write(log, [{"type": "user", "message": {"content": "hi"}}])
    t.update()
    assert agentinfo.cache(t) is None and agentinfo.cache_label(None) == ""
    reply = _assistant([{"type": "text", "text": "ok"}])
    reply["timestamp"] = "2026-10-04T16:00:00.000Z"
    reply["message"]["usage"]["cache_creation"] = {"ephemeral_1h_input_tokens": 50, "ephemeral_5m_input_tokens": 0}
    _write(log, [reply])
    t.update()
    at = t.cache_at
    assert agentinfo.cache(t, at + 600) == {"warm": True, "left": 3000, "ttl": 3600}
    assert agentinfo.cache_label(agentinfo.cache(t, at + 600)) == "cache warm · 50m left"
    assert agentinfo.cache_label(agentinfo.cache(t, at + 3570)) == "cache warm · 30s left"
    assert agentinfo.cache_label(agentinfo.cache(t, at + 3601)) == "cache cold"
    read_only = _assistant([{"type": "text", "text": "again"}])  # a pure read keeps the lifetime it had
    read_only["timestamp"] = "2026-10-04T17:00:00.000Z"
    _write(log, [read_only])
    t.update()
    assert t.cache_ttl == 3600 and t.cache_at == at + 3600
    short = _assistant([{"type": "text", "text": "5m"}])
    short["timestamp"] = "not a time"
    short["message"]["usage"]["cache_creation"] = {"ephemeral_5m_input_tokens": 9}
    _write(log, [short])
    t.update()
    assert t.cache_ttl == 300 and t.cache_at == at + 3600  # a bad timestamp keeps the last good one
def _sub(claude, name, model=None, meta=None):
    sub = claude.log.parent / SID / "subagents"
    (sub / f"agent-{name}.meta.json").write_text(json.dumps(meta or {"agentType": "Explore", "description": name}))
    (sub / f"agent-{name}.jsonl").write_text("")
    if model:
        _write(sub / f"agent-{name}.jsonl", [_assistant([], model=model)])


def test_subagent_model_comes_from_its_own_transcript_over_the_alias(ws, claude):
    _sub(claude, "c", model="claude-sonnet-5-5", meta={"agentType": "x", "description": "c", "model": "opus"})
    by = {s["description"]: s["model"] for s in agentinfo.info(ws, claude.session)["subagents"]}
    assert by["c"] == "Sonnet 5.5"
    assert by["Review L-0008"] == "sonnet"  # no assistant message yet: the caller's alias stays


def test_session_models_groups_main_and_subagents(claude):
    _sub(claude, "c", model="claude-sonnet-5-5")
    _sub(claude, "d", model="claude-sonnet-5-5")
    m = agentinfo.session_models(SID)
    assert m["main"] == [("Opus 5.5", 1)] and m["total"] == 4
    assert dict(m["subagents"])["Sonnet 5.5"] == 2
    assert agentinfo.session_models("nope") is None


def _ran_ticket(put, harness="claude-code", sid=SID):
    return put("in-progress", sessions=[{"id": sid, "harness": harness, "model": None, "started": "2026-10-04T09:00:00Z"}])


def test_ticket_page_shows_ran_on_models(dash, put, claude):
    _sub(claude, "c", model="claude-sonnet-5-5")
    html = dash.get(f"/t/{_ran_ticket(put)}").text
    assert 'id="ran-on"' in html and "Opus 5.5" in html and "1 × Sonnet 5.5" in html


def test_ticket_page_ran_on_missing_transcript_and_other_harness(dash, put, claude):
    html = dash.get(f"/t/{_ran_ticket(put, sid='gone')}").text
    assert "not on this machine" in html
    html = dash.get(f"/t/{_ran_ticket(put, harness='codex')}").text
    assert 'id="ran-on"' in html and "codex" in html and "not on this machine" not in html


def test_ticket_page_without_sessions_has_no_ran_on(dash, put, claude):
    assert 'id="ran-on"' not in dash.get(f"/t/{put('open')}").text
