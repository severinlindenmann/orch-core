"""Context budget tests (core §5): what an agent is given stays within fixed sizes. Tokens are estimated as four
characters each, a deliberately simple rule that cannot flatter the text."""

from __future__ import annotations

import io

import pytest

from orch.cli.main import main
from orch.instructions import (
    AGENTS_MAX_LINES,
    SESSION_START_MAX_LINES,
    SKILL_MAX_CHARS,
    builtin_skills,
    render_agents_md,
    session_start_lines,
)
from tests.cli.conftest import run_cli
from tests.cli.test_describe import MAX_HELP_BYTES, MAX_HELP_LINES
from tests.ops.helpers import Cli, Ws

TOKENS = lambda text: -(-len(text) // 4)  # noqa: E731

AGENTS_MAX_TOKENS = 300  # loaded every session
SESSION_START_MAX_TOKENS = 150  # loaded every session start and after every compaction
SKILL_MAX_TOKENS = 600  # loaded only when the task matches
ALWAYS_LOADED_MAX_TOKENS = 450


def test_agents_md_is_at_most_25_lines_and_300_tokens():
    text = render_agents_md()
    assert len(text.splitlines()) <= AGENTS_MAX_LINES == 25
    assert TOKENS(text) <= AGENTS_MAX_TOKENS, TOKENS(text)


def test_each_skill_is_under_its_token_budget():
    assert SKILL_MAX_CHARS // 4 <= SKILL_MAX_TOKENS
    for s in builtin_skills():
        assert TOKENS(s.text) <= SKILL_MAX_TOKENS, (s.name, TOKENS(s.text))


WORST_SESSION_START = session_start_lines(
    person="A Person With A Long Display Name",
    grant="gr_01J9Z8AAAAAAAAAAAAAAAAAAAA until 2026-10-11T18:00:00Z",
    claim="DEMO-0043",
    claim_line="DEMO-0043 in-progress (your claim) \u00b7 T3 next \u00b7 99 new events \u00b7 waiting: Q1 blocking",
    unread=["DEMO-0043 #14 answered Q1 option=b"] * 9,
    stale=True,
    next_hint="orch wait",
)


def test_the_worst_session_start_text_is_six_lines_and_150_tokens():
    assert len(WORST_SESSION_START) <= SESSION_START_MAX_LINES == 6
    assert TOKENS("\n".join(WORST_SESSION_START)) <= SESSION_START_MAX_TOKENS


def test_always_loaded_text_together_stays_small():
    total = TOKENS(render_agents_md()) + TOKENS("\n".join(WORST_SESSION_START))
    assert total <= ALWAYS_LOADED_MAX_TOKENS, total


def test_the_real_session_start_output_in_a_busy_workspace_stays_within_six_lines(tmp_path):
    ws = Ws(tmp_path)
    ws.bootstrap()
    try:
        cli = Cli(ws)
        cli("new", "A ticket")
        cli("claim", "1")
        for i in range(4):
            cli("ask", f"Question number {i}?", "--options", "a,b", "--non-blocking")
        out = io.StringIO()
        code = main(
            ["instructions", "hook", "session-start"],
            env=ws.env(),
            stdout=out,
            stderr=io.StringIO(),
            now=lambda: ws.clock[0],
        )
        assert code == 0 and len(out.getvalue().splitlines()) <= 6 and TOKENS(out.getvalue()) <= 150
    finally:
        ws.store.close()


@pytest.mark.parametrize(
    "argv", [["help"], ["help", "work"], ["help", "ask"], ["help", "refine"], ["help", "parallel"]]
)
def test_orch_help_keeps_its_pinned_budget(argv):
    out = run_cli(*argv).out
    assert len(out.splitlines()) <= MAX_HELP_LINES and len(out.encode()) <= MAX_HELP_BYTES
