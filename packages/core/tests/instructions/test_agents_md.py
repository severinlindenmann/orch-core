"""AGENTS.orch.md: the generated text, its budget, and that it cannot drift from the registry."""

from __future__ import annotations

from pathlib import Path

import pytest

import orch.ops as ops
from orch.instructions import AGENTS_MAX_LINES, INSTRUCTIONS_REV, render_agents_md, stamp_rev
from orch.ops import workflows
from tests.instructions.helpers import command_references, unknown_references

GOLDEN = Path(__file__).parent / "golden" / "AGENTS.orch.md"


def test_the_text_is_pinned(request):
    text = render_agents_md()
    if request.config.getoption("--update-golden"):
        GOLDEN.write_text(text)
    assert text == GOLDEN.read_text(), "run: uv run pytest tests/instructions/test_agents_md.py --update-golden"


def test_at_most_40_lines_with_headings_each_short_and_stamped_under_the_title():
    text = render_agents_md()
    lines = text.splitlines()
    assert 1 <= len(lines) <= AGENTS_MAX_LINES == 40
    assert text.endswith("\n") and "\r" not in text
    assert all(len(x) <= 110 for x in lines), [x for x in lines if len(x) > 110]
    assert lines[0] == "# orch in this project" and lines[1].startswith("orch v2.0 (instructions r")
    assert stamp_rev(text) == INSTRUCTIONS_REV == 5
    assert [x for x in lines if x.startswith("#")] == [
        "# orch in this project",
        "## First, every session",
        "## Working on a ticket",
        "## When you are unsure",
        "## Never",
        "## If orch refuses",
        "## More",
    ]


def test_it_points_to_the_registry_instead_of_explaining_it():
    text = render_agents_md()
    assert "orch help work" in text and "orch describe <cmd>" in text
    first = text.split("## First, every session")[1].splitlines()[1]
    assert "orch status" in first  # start here, also for harnesses with no hook


def test_every_command_and_flag_it_names_exists():
    assert unknown_references(render_agents_md()) == []
    mentioned = {op.name for _m, op, _f in command_references(render_agents_md()) if op}
    assert {
        "status",
        "claim",
        "task.next",
        "task.done",
        "artifact.add",
        "ask",
        "wait",
        "handoff",
        "submit",
    } <= mentioned
    assert {"task.start", "log", "grant", "help", "describe"} <= mentioned


def test_the_example_calls_come_from_the_workflows():
    text = render_agents_md()
    for op_name in ("claim", "task.next", "task.done"):
        call = next(c for n, c in workflows.WORKFLOWS["work"][1] if n == op_name)
        assert " ".join(call.split()[:5]) in text


def test_a_renamed_workflow_step_fails_the_generator_not_the_reader(monkeypatch):
    broken = dict(workflows.WORKFLOWS)
    one, steps = broken["work"]
    broken["work"] = (one, [s for s in steps if s[0] != "task.next"])
    monkeypatch.setattr("orch.instructions.agents_md.WORKFLOWS", broken)
    with pytest.raises(KeyError):
        render_agents_md()


def test_a_command_missing_from_the_registry_fails_the_generator(monkeypatch):
    real = ops.get

    def get(name):
        if name == "artifact.add":
            raise KeyError(name)
        return real(name)

    monkeypatch.setattr(ops, "get", get)
    with pytest.raises(KeyError):
        render_agents_md()


def test_stamp_rev_reads_only_a_well_formed_first_line():
    assert stamp_rev("orch v2.0 (instructions r7) · x\nmore") == 7
    assert stamp_rev("# orch in this project\norch v2.0 (instructions r2) · x") == 2  # r2 sits under the title
    assert stamp_rev("a\nb\nc\norch v2.0 (instructions r2)") is None
    assert stamp_rev("hello") is None and stamp_rev("") is None


def test_agents_never_ask_for_a_gate_approval():
    """R3-C4d: a finished gate reaches the person in Approvals; asking them to approve it is a duplicate."""
    from orch.instructions.skills import builtin_skills

    text = render_agents_md()
    assert "Never ask to approve a gate" in text
    for skill in builtin_skills():
        body = skill.text.lower()
        assert "ask the person to review" not in body and "say what and why with `orch ask`" not in body
    bodies = {s.name: s.text for s in builtin_skills()}
    assert "Never ask a question" in bodies["orch-refine-ticket"] and "orch wait" in bodies["orch-refine-ticket"]
    assert "do not ask them to approve or review it" in bodies["orch-tickets"]
