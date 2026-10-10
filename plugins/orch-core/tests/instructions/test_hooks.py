"""Session-start and pre-compact hook text: who you work for, your claim, unread decisions, next step."""

from __future__ import annotations

import io
import itertools
import json

import pytest

from orch.cli.main import main
from orch.instructions import (
    PRE_COMPACT_MAX_LINES,
    SESSION_START_MAX_LINES,
    plugin_files,
    pre_compact_lines,
    session_start_lines,
)
from tests.ops.helpers import Cli, Ws


def lines(**kw):
    base = {
        "person": "Severin",
        "grant": "gr_01J9Z8 until 2026-10-11T18:00:00Z",
        "claim": "DEMO-0043",
        "claim_line": "DEMO-0043 in-progress (your claim) · T3 next · 2 new events",
        "unread": [],
        "stale": False,
        "next_hint": "orch task next",
    }
    return session_start_lines(**{**base, **kw})


def test_the_session_start_text_in_the_normal_case():
    assert lines() == [
        "ok session-start Severin · grant gr_01J9Z8 until 2026-10-11T18:00:00Z",
        "DEMO-0043 in-progress (your claim) · T3 next · 2 new events",
        "next: orch task next",
    ]


@pytest.mark.parametrize(
    ("grant", "claimed", "unread", "stale"),
    list(itertools.product([None, "gr_1 until x"], [True, False], [0, 1, 2, 9], [True, False])),
)
def test_never_more_than_six_lines_and_always_an_ok_line_and_a_next_line(grant, claimed, unread, stale):
    out = lines(
        grant=grant,
        claim="DEMO-1" if claimed else None,
        claim_line="DEMO-1 in-progress (your claim)" if claimed else None,
        unread=[f"DEMO-1 #{i} answered Q{i}" for i in range(unread)],
        stale=stale,
    )
    assert len(out) <= SESSION_START_MAX_LINES == 6
    assert out[0].startswith("ok session-start ") and out[-1].startswith("next: ")
    assert ("instructions stale: run orch instructions sync" in out) is stale
    assert (not grant) == any(x.startswith("no grant:") for x in out)
    assert any(x.startswith("unread: ") for x in out) is bool(unread)
    assert ("no claim" in out) is (not claimed)


def test_unread_decisions_are_cut_to_two_with_a_count():
    out = lines(unread=["a", "b", "c", "d"])
    assert "unread: a; b; +2 more (orch wait)" in out


def test_pre_compact_text_is_short_and_says_what_to_keep():
    out = pre_compact_lines(claim="DEMO-0043", open_questions=2)
    assert len(out) <= PRE_COMPACT_MAX_LINES == 4
    assert out[0].startswith("ok pre-compact") and out[-1].startswith("next: ")
    assert any("DEMO-0043" in x for x in out) and any("2 question" in x for x in out)
    assert len(pre_compact_lines(claim=None, open_questions=0)) <= 4


# ------------------------------------------------------------------------------------------------ through the command


@pytest.fixture
def ws(tmp_path):
    w = Ws(tmp_path)
    w.bootstrap()
    yield w
    w.store.close()


def hook(ws, event, **env):
    out, err = io.StringIO(), io.StringIO()
    code = main(
        ["instructions", "hook", event],
        env={**ws.env(), **env},
        stdout=out,
        stderr=err,
        now=lambda: ws.clock[0],
    )
    return code, out.getvalue(), err.getvalue()


def test_the_hook_names_the_person_the_grant_the_claim_and_the_next_step(ws):
    cli = Cli(ws)
    cli("new", "A ticket")
    cli("claim", "1")
    code, out, err = hook(ws, "session-start")
    got = out.splitlines()
    assert code == 0 and len(got) <= 6, out + err
    assert got[0].startswith("ok session-start Owner · grant gr_")
    assert any(x.startswith("DEMO-0001 ") and "(your claim)" in x for x in got)
    assert got[-1].startswith("next: orch ")
    assert "instructions stale: run orch instructions sync" in got  # nothing installed in the test workspace


def test_an_unread_decision_shows_up_and_wait_clears_it(ws):
    cli = Cli(ws)
    cli("new", "A ticket")
    cli("claim", "1")
    assert cli("ask", "Which?", "--options", "a,b").code == 0
    q = ws.view("1").questions[0]
    ws.sign("1", "question.answered", question="Q1", hash=q.hash, option="b", text="because")
    out = hook(ws, "session-start")[1]
    assert any(x.startswith("unread: DEMO-0001 #") and "answered Q1" in x for x in out.splitlines()), out
    assert len(out.splitlines()) <= 6
    assert cli("wait", "--timeout", "2").code == 0
    assert "unread:" not in hook(ws, "session-start")[1]


def test_without_a_grant_it_says_what_works(ws):
    out = hook(ws, "session-start", ORCH_GRANT="")[1].splitlines()
    assert out[0] == "ok session-start unattended · no grant" or out[0].startswith("ok session-start ")
    assert any(x.startswith("no grant:") for x in out) and len(out) <= 6


def test_pre_compact_through_the_command(ws):
    cli = Cli(ws)
    cli("new", "A ticket")
    cli("claim", "1")
    cli("ask", "Which?", "--options", "a,b")
    code, out, _ = hook(ws, "pre-compact")
    got = out.splitlines()
    assert code == 0 and got[0].startswith("ok pre-compact") and len(got) <= PRE_COMPACT_MAX_LINES
    assert any("DEMO-0001" in x for x in got)


def test_outside_a_workspace_a_hook_says_nothing_and_succeeds(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    for event in ("session-start", "pre-compact"):
        out, err = io.StringIO(), io.StringIO()
        code = main(["instructions", "hook", event], env={"HOME": str(tmp_path)}, stdout=out, stderr=err)
        assert (code, out.getvalue(), err.getvalue()) == (0, "", ""), event


def test_the_plugin_runs_exactly_these_two_hooks():
    hooks = json.loads(plugin_files()["hooks/hooks.json"])["hooks"]
    assert set(hooks) == {"SessionStart", "PreCompact"}
    cmds = [h["command"] for g in hooks.values() for e in g for h in e["hooks"]]
    assert cmds == ["orch instructions hook session-start", "orch instructions hook pre-compact"]
    assert hooks["SessionStart"][0]["matcher"] == "startup|resume|clear|compact"  # re-injected after compaction
