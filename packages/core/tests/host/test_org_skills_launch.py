"""M3 host H-3: which launches carry company skills (Opus conditions 12 to 14 and decision 7). Only a Claude Code
agent in a ticket work folder has the appended rules file, so only it gets the accepted skills; Codex, worktree,
repo, root and shell launches get none (the host does not even ask), and any doubt reading the accepted set is none."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from orch.host import agents, orgstate
from orch.host.launch import RULES_FLAG, RULES_PLACEHOLDER, launch_argv
from orch.runner.config import parse

LOGIN = parse({"v": 1})
from orch.hubclient.orgbundles import skill_digest  # noqa: E402

SKILL = {
    "name": "invoices",
    "text": "Write INV-2026-0001.",
    "digest": skill_digest("invoices", "Write INV-2026-0001."),
    "extra": "dropped",
}


def run(argv):
    return asyncio.run(agents._company_skills(SimpleNamespace(service=SimpleNamespace()), argv))  # noqa: SLF001


@pytest.fixture
def accepted(monkeypatch):
    calls = []

    def served(service):
        calls.append(service)
        return {"skills": [SKILL], "mcp": []}

    monkeypatch.setattr(orgstate, "served", served)
    return calls


def test_a_claude_code_ticket_work_folder_agent_gets_the_accepted_skills(accepted):
    argv, _, _ = launch_argv("claude-code", "work", None, "ORC-7", LOGIN, workdir=True)
    assert RULES_PLACEHOLDER in argv
    assert run(argv) == [
        {"name": "invoices", "text": "Write INV-2026-0001.", "digest": SKILL["digest"]}
    ]  # only 3 members
    assert len(accepted) == 1


@pytest.mark.parametrize(
    ("harness", "workdir", "what"),
    [
        ("codex", True, "a Codex run"),
        ("codex", False, "a Codex run outside a work folder"),
        ("claude-code", False, "a Claude Code worktree, repo or root launch"),
        ("shell", False, "a shell"),
    ],
)
def test_every_other_launch_gets_none_and_the_host_does_not_even_read_them(accepted, harness, workdir, what):
    mode = None if harness == "shell" else "work"
    argv, _, _ = launch_argv(harness, mode, None, None if harness == "shell" else "ORC-7", LOGIN, workdir=workdir)
    assert RULES_PLACEHOLDER not in argv, what
    assert run(argv) == [] and accepted == []


def test_a_misplaced_or_doubled_placeholder_is_none(accepted):
    for argv in (
        ["claude", RULES_PLACEHOLDER],
        ["claude", RULES_FLAG, RULES_PLACEHOLDER, RULES_PLACEHOLDER],
        [RULES_PLACEHOLDER],
    ):
        assert run(argv) == []
    assert accepted == []


def test_any_doubt_reading_the_accepted_set_is_none_not_a_failed_launch(monkeypatch):
    def boom(service):
        raise OSError("custody.unsafe_mode")

    monkeypatch.setattr(orgstate, "served", boom)
    argv, _, _ = launch_argv("claude-code", "work", None, "ORC-7", LOGIN, workdir=True)
    assert run(argv) == []


def test_nothing_accepted_is_an_empty_list(monkeypatch):
    monkeypatch.setattr(orgstate, "served", lambda service: {"skills": [], "mcp": []})
    argv, _, _ = launch_argv("claude-code", "work", None, "ORC-7", LOGIN, workdir=True)
    assert run(argv) == []


def test_a_skill_the_runner_would_refuse_is_dropped_with_a_log_line_not_the_launch(monkeypatch, caplog):
    from orch.hubclient.orgbundles import skill_digest
    from orch.runner import rules

    def mk(name, text):
        return {"name": name, "text": text, "digest": skill_digest(name, text)}

    header = rules.COMPANY_HEADER.strip().splitlines()[0]
    good = mk("invoices", "Intro\n## Steps\n1. Do it.")  # headings are fine: inert inside the runner's fence
    bad = mk("faker", "x\n" + header)  # would fake the start of the company section
    fencey = mk("fencey", "intro\n~~~\nlooks like an end")  # a line of tildes may look like the end of its fence
    bad_digest = {**mk("tampered", "ok"), "digest": "sha256:" + "0" * 64}
    monkeypatch.setattr(orgstate, "served", lambda service: {"skills": [bad, fencey, good, bad_digest], "mcp": []})
    argv, _, _ = launch_argv("claude-code", "work", None, "ORC-7", LOGIN, workdir=True)
    with caplog.at_level("WARNING"):
        got = run(argv)
    assert [s["name"] for s in got] == ["invoices"]  # the launch goes on with what is acceptable
    assert (
        "faker" in caplog.text
        and "fencey" in caplog.text
        and "tampered" in caplog.text
        and "invoices" not in caplog.text
    )
    assert rules.company_problem(got) is None  # exactly what the runner will take
