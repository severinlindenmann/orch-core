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


def test_at_most_25_lines_each_short_and_stamped_first():
    text = render_agents_md()
    lines = text.splitlines()
    assert 1 <= len(lines) <= AGENTS_MAX_LINES == 25
    assert text.endswith("\n") and "\r" not in text
    assert all(len(x) <= 110 for x in lines), [x for x in lines if len(x) > 110]
    assert lines[0].startswith("orch v2.0 (instructions r")
    assert stamp_rev(text) == INSTRUCTIONS_REV
    assert "tickets only through `orch`" in lines[0] and "never edit tickets/**" in lines[0]


def test_it_points_to_the_registry_instead_of_explaining_it():
    text = render_agents_md()
    assert "orch help work" in text and "orch describe <cmd>" in text
    assert "orch status" in text.splitlines()[1]  # start here, also for harnesses with no hook


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


def test_addon_lines_fit_the_budget_and_go_before_the_last_line():
    text = render_agents_md(("dashboard: orch addon list",))
    lines = text.splitlines()
    assert lines[-2] == "dashboard: orch addon list" and lines[-1].startswith("more: ")
    with pytest.raises(ValueError, match="limit is 25"):
        render_agents_md(tuple(f"addon {i}: x" for i in range(AGENTS_MAX_LINES)))
    with pytest.raises(ValueError):
        render_agents_md(("two\nlines",))


def test_stamp_rev_reads_only_a_well_formed_first_line():
    assert stamp_rev("orch v2.0 (instructions r7) · x\nmore") == 7
    assert stamp_rev("hello") is None and stamp_rev("") is None
    assert stamp_rev("line\norch v2.0 (instructions r7)") is None
