from __future__ import annotations

import json

import orch.ops as ops
from orch import schema
from orch.cli.meta import overview
from orch.ops.workflows import WORKFLOWS

MAX_DESCRIBE_LINES = 28
MAX_DESCRIBE_BYTES = 1700
MAX_HELP_LINES = 10
MAX_HELP_BYTES = 800
MAX_OVERVIEW_LINES = 8
MAX_OP_HELP_LINES = 14


def test_describe_one_command_in_every_spelling(cli):
    a = cli("describe", "task", "done").out
    assert a == cli("describe", "task.done").out
    assert cli("describe", "request-changes").out == cli("describe", "request_changes").out != ""
    lines = a.splitlines()
    assert lines[0] == "ok describe task.done"
    assert lines[2].startswith("usage: orch task done TASK ")
    assert "who: agent · emits: task.done,artifact.added" in lines
    assert any(line.startswith("  verify.failed (exit 5)") for line in lines)
    assert lines[-1].startswith("out: ok {key} task.done {task}")


def test_describe_json_is_the_registry_entry(cli):
    r = cli("describe", "task.done", "--json")
    env = json.loads(r.out)
    schema.validate("cli-result", env)
    d = env["data"]
    op = ops.get("task.done")
    assert d["name"] == "task.done" and d["emits"] == op.emits and d["pre"] == op.pre
    assert d["input"] == op.input and d["output"] == op.output
    assert [e["code"] for e in d["errors"]] == [e["code"] for e in op.errors]
    assert all({"code", "exit", "retryable", "hint", "fix"} == set(e) for e in d["errors"])


def test_describe_without_argument_lists_the_commands_by_group(cli):
    r = cli("describe")
    lines = r.out.splitlines()
    assert lines[0] == "ok describe all" and lines[-1] == "next: orch describe CMD"
    groups = [line.split(":")[0] for line in lines[1:-1]]
    assert groups == ["Context", "Read", "Lifecycle", "Edit", "Human only", "Admin"]
    assert "Edit: set, section set, ac add|edit, task list|next|add|start|done|skip|block|reopen" in r.out
    names = {c["name"] for c in json.loads(cli("describe", "--json").out)["data"]["commands"]}
    assert names == set(ops.names())


def test_describe_unknown_command(cli):
    r = cli("describe", "frob")
    assert r.code == 2 and r.err.startswith("err not_found no command 'frob'")


def test_help_lists_workflows_and_each_workflow_is_generated_from_the_registry(cli):
    r = cli("help")
    assert r.out.splitlines()[0] == "ok help all"
    for name in WORKFLOWS:
        assert any(line.startswith(f"{name}: ") for line in r.out.splitlines())
    w = cli("help", "work").out.splitlines()
    assert w[0] == "ok help work"
    assert w[-1] == "next: orch describe CMD"
    claim = ops.get("claim")
    assert any(
        line.startswith("2. orch claim --next") and claim.summary.split(":")[0].split(";")[0].rstrip(".") in line
        for line in w
    )
    j = json.loads(cli("help", "work", "--json").out)
    schema.validate("cli-result", j)
    assert [s["op"] for s in j["data"]["steps"]] == [op for op, _ in WORKFLOWS["work"][1]]
    r = cli("help", "nonsense")
    assert r.code == 2 and "err not_found no workflow 'nonsense'" in r.err


def test_empty_command_line_is_help(cli):
    assert cli().out == cli("help").out


def test_version_is_kept(cli):
    r = cli("--version")
    assert (r.code, r.out) == (0, "orch v2 (in development)\n")


def test_command_help_flag(cli):
    r = cli("task", "done", "--help")
    assert r.code == 0
    assert r.out.splitlines()[0] == ops.get("task.done").summary
    assert "usage: orch task done TASK" in r.out
    assert cli("-h").out == cli("help").out


# ---- context budget (core 5): help and describe stay small


def test_describe_size_budget_per_command(cli):
    for op in ops.all():
        text = cli("describe", op.name).out
        assert len(text.splitlines()) <= MAX_DESCRIBE_LINES, op.name
        assert len(text.encode()) <= MAX_DESCRIBE_BYTES, op.name


def test_help_and_overview_size_budget(cli):
    assert len(cli("help").out.splitlines()) <= MAX_HELP_LINES
    assert len(cli("help").out.encode()) <= MAX_HELP_BYTES
    assert len(overview()) <= MAX_OVERVIEW_LINES
    assert len(cli("describe").out.splitlines()) <= MAX_OVERVIEW_LINES + 2
    for name in WORKFLOWS:
        assert len(cli("help", name).out.splitlines()) <= MAX_HELP_LINES
        assert len(cli("help", name).out.encode()) <= MAX_HELP_BYTES
    for op in ops.all():
        assert len(cli(*op.words, "--help").out.splitlines()) <= MAX_OP_HELP_LINES, op.name


def test_output_is_plain_ascii_free_of_colour_codes(cli):
    for argv in (["help"], ["describe", "claim"], ["describe", "--json"]):
        assert "\x1b" not in cli(*argv).out
