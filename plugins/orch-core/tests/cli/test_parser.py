from __future__ import annotations

import pytest

import orch.ops as ops
from orch.cli import parser
from orch.cli.args import arg_specs, usage
from orch.cli.errors import UsageError
from orch.ops import _dsl as d
from orch.ops.errors import OrchError


def probe():
    return d.operation(
        "probe.run",
        "Edit",
        "A probe.",
        who="agent",
        props={
            "target": d.S("the target", **{"x-metavar": "TARGET"}),
            "tags": d.L("tags", **{"x-metavar": "A,B"}),
            "count": d.I("how many", default=3),
            "verbose": d.B("talk"),
            "mode": d.E("mode", "fast", "slow"),
            "long_name": d.S("hyphenated flag"),
            "message": d.MSG,
            "pairs": d.L("key=value pairs", **{"x-metavar": "KEY=VALUE"}),
        },
        required=("target",),
        positional=("target", "pairs"),
        text="ok probe",
    )


def parse_with(op, *argv):
    p = parser.build_parser(op)
    return vars(p.parse_args(list(argv)))


def test_flags_and_positionals_come_from_the_schema():
    specs = {a.name: a for a in arg_specs(probe())}
    assert specs["target"].positional and specs["target"].required
    assert specs["pairs"].positional and not specs["pairs"].required and specs["pairs"].kind == "list"
    assert specs["long_name"].flag == "--long-name"
    assert specs["message"].short == "m"
    assert specs["count"].kind == "int" and specs["count"].default == 3
    assert specs["mode"].choices == ("fast", "slow")
    assert specs["verbose"].kind == "bool"


def test_usage_line():
    assert usage(probe()) == (
        "orch probe run TARGET [KEY=VALUE...] [--tags A,B] [--count COUNT] [--verbose] [--mode fast|slow]"
        " [--long-name LONG_NAME] [--message TEXT] [--dry-run]"
    )


def test_values_lists_ints_switches_and_short_flags():
    ns = parse_with(probe(), "x", "a=1", "b=2", "--tags", "p,q", "--tags", "r", "--count", "7", "--verbose", "-m", "hi")
    assert ns["target"] == "x" and ns["pairs"] == ["a=1", "b=2"]
    assert ns["tags"] == ["p", "q", "r"] and ns["count"] == 7 and ns["verbose"] is True and ns["message"] == "hi"


def test_missing_required_and_bad_values_are_usage_errors():
    p = parser.build_parser(probe())
    for argv in ([], ["x", "--count", "many"], ["x", "--mode", "medium"], ["x", "--nope"], ["x", "--tag", "a"]):
        with pytest.raises(UsageError) as e:
            p.parse_args(argv)
        assert e.value.code == "usage" and e.value.fix == ["orch", "describe", "probe.run"]


def test_no_abbreviated_flags():
    with pytest.raises(UsageError):
        parser.build_parser(probe()).parse_args(["x", "--verb"])


def test_dry_run_only_on_writes():
    assert "dry_run" in parse_with(ops.get("log"), "note")
    with pytest.raises(UsageError):
        parser.build_parser(ops.get("show")).parse_args(["--dry-run"])


def test_command_words_resolve_longest_first():
    assert parser.parse(["task", "done", "T3"]).op.name == "task.done"
    assert parser.parse(["task.done", "T3"]).op.name == "task.done"
    assert parser.parse(["show", "43"]).op.name == "show"
    assert parser.parse(["grant", "revoke", "gr_01J9ZK4Q7M3R8T2V6X0B5N1C9D"]).op.name == "grant.revoke"
    assert parser.parse(["grant", "--hours", "2"]).op.name == "grant"
    assert parser.parse(["request-changes", "plan", "-m", "x"]).op.name == "request_changes"
    assert parser.parse(["import", "v1", "/tmp/x"]).args == {"path": "/tmp/x"}


def test_parse_result_drops_absent_arguments_and_applies_defaults():
    got = parser.parse(["task", "done", "T3", "--run", "--ac", "AC2", "-m", "ok"])
    assert got.args == {"task": "T3", "run": True, "ac": "AC2", "message": "ok"}
    assert parser.parse(["wait"]).args == {"timeout": 540}
    assert parser.parse(["list", "--mine"]).args == {"mine": True, "limit": 20}
    assert parser.parse(["ask", "why?", "--options", "a,b", "--rec", "a"]).args == {
        "text": "why?",
        "options": ["a", "b"],
        "rec": "a",
    }
    assert parser.parse(["log", "x", "--dry-run"]).dry_run is True


def test_json_flag_is_taken_out_anywhere():
    for argv in (["--json", "list"], ["list", "--json"], ["list", "--mine", "--json"]):
        got = parser.parse(argv)
        assert got.json and got.op.name == "list"


def test_unknown_and_incomplete_commands():
    with pytest.raises(OrchError) as e:
        parser.parse(["frobnicate"])
    assert e.value.code == "unknown_command"
    with pytest.raises(UsageError) as u:
        parser.parse(["task"])
    assert "add, block, done" in u.value.message
    with pytest.raises(UsageError):
        parser.parse([])


def test_every_registered_operation_builds_a_parser():
    for op in ops.all():
        parser.build_parser(op)
        assert usage(op).startswith("orch " + op.cli)
