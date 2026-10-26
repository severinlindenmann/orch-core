"""Focus phase 2, P2-4: what a run read (web pages, searches, files), from hand-written synthetic transcripts."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from orch.runner import transcript, transcript_sources
from orch.runner.supervise import SpawnError

FIX = Path(__file__).parent / "fixtures" / "transcripts"
CWD = "/work/proj"
RUN = "r_01J0000000000000000000000A"


def test_claude_sources_are_deduped_newest_first_and_inside_the_cwd():
    got = transcript_sources.sources(FIX / "claude_research.jsonl", "claude-code", CWD)
    by_kind = {(s["kind"], s.get("url") or s.get("query") or s.get("path")): s for s in got}
    assert set(by_kind) == {
        ("web", "https://www.example.org/rfc/6585#section-4"), ("search", "redis rate limit counter"),
        ("file", "docs/api/invites.md"),
    }  # fmt: skip  (/etc/hosts is outside the cwd, the failed fetch is dropped, the repeats are merged)
    web = by_kind[("web", "https://www.example.org/rfc/6585#section-4")]
    assert web["host"] == "example.org" and web["has_extract"] is True and web["read_at"] == "2026-10-26T10:05:00Z"
    assert by_kind[("file", "docs/api/invites.md")]["read_at"] == "2026-10-26T10:05:00Z"
    assert [s["read_at"] for s in got] == sorted((s["read_at"] for s in got), reverse=True)
    search = by_kind[("search", "redis rate limit counter")]
    assert search["results"] == [{"title": "INCR | Redis", "url": "https://redis.example.io/commands/incr"}]


def test_a_search_without_structured_hits_lists_the_urls_in_its_text():
    recs = [
        {"type": "assistant", "timestamp": "t1", "message": {"content": [
            {"type": "tool_use", "id": "q", "name": "WebSearch", "input": {"query": "q"}}]}},
        {"type": "user", "timestamp": "t2", "message": {"content": [
            {"type": "tool_result", "tool_use_id": "q", "content": "a https://one.example/x b https://one.example/x"}]}},
    ]  # fmt: skip
    (s,) = transcript_sources._claude(recs, CWD, False)
    assert s["results"] == [{"title": None, "url": "https://one.example/x"}]


def test_extract_is_the_text_the_agent_got_and_only_for_web_sources():
    path = FIX / "claude_research.jsonl"
    rows = transcript_sources.sources(path, "claude-code", CWD)
    web = next(s for s in rows if s["kind"] == "web")
    out = transcript_sources.extract(path, "claude-code", web["id"], CWD)
    assert out == {"id": web["id"], "url": web["url"], "extract": "Newer: 429 with Retry-After.", "truncated": False}
    for other in (next(s for s in rows if s["kind"] == "file")["id"], "s_0000000000"):
        with pytest.raises(SpawnError) as e:
            transcript_sources.extract(path, "claude-code", other, CWD)
        assert e.value.code == "not_found"


def test_extract_is_capped(tmp_path):
    import json

    big = "x" * (transcript_sources.MAX_EXTRACT + 10)
    f = tmp_path / "t.jsonl"
    f.write_text("\n".join(json.dumps(r) for r in [
        {"type": "assistant", "timestamp": "t1", "message": {"content": [
            {"type": "tool_use", "id": "w", "name": "WebFetch", "input": {"url": "https://a.example/"}}]}},
        {"type": "user", "timestamp": "t2", "message": {"content": [
            {"type": "tool_result", "tool_use_id": "w", "content": big}]}},
    ]) + "\n")  # fmt: skip
    (row,) = transcript_sources.sources(f, "claude-code", CWD)
    out = transcript_sources.extract(f, "claude-code", row["id"], CWD)
    assert len(out["extract"]) == transcript_sources.MAX_EXTRACT and out["truncated"] is True


def test_codex_sources_have_no_extract():
    got = transcript_sources.sources(FIX / "codex_research.jsonl", "codex", CWD)
    kinds = {s["kind"]: s for s in got}
    assert kinds["search"]["query"] == "idempotency keys"
    assert kinds["web"]["url"] == "https://docs.example.com/idem" and kinds["web"]["has_extract"] is False


def test_serve_lists_and_reads_one_source(tmp_path):
    cfg = tmp_path / "cfg"
    proj = cfg / "projects" / "-work-proj"
    proj.mkdir(parents=True)
    (proj / f"{transcript.session_id(RUN)}.jsonl").write_bytes((FIX / "claude_research.jsonl").read_bytes())
    sup = SimpleNamespace(table={RUN: {"harness": "claude-code", "cwd_path": CWD}})
    env = {"CLAUDE_CONFIG_DIR": str(cfg), "HOME": str(tmp_path)}
    listed = transcript_sources.serve({"run": RUN, "sources": True}, sup, env)
    assert listed["available"] is True and len(listed["sources"]) == 3
    web = next(s for s in listed["sources"] if s["kind"] == "web")
    one = transcript_sources.serve({"run": RUN, "sources": True, "source": web["id"]}, sup, env)
    assert one["extract"].startswith("Newer")
    with pytest.raises(SpawnError):
        transcript_sources.serve({"run": RUN, "sources": True, "source": "bad"}, sup, env)
    with pytest.raises(SpawnError):
        transcript_sources.serve({"run": "r_other", "sources": True}, sup, env)
    none = transcript_sources.serve({"run": RUN, "sources": True}, sup, {"CLAUDE_CONFIG_DIR": str(tmp_path / "no")})
    assert none["available"] is False and none["sources"] == []
