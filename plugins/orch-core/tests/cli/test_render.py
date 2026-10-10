from __future__ import annotations

import io
import json

import pytest

import orch.ops as ops
from orch import schema
from orch.cli import render
from orch.ops import Result
from orch.ops.errors import OrchError


def test_fill_required_optional_and_next():
    t = "ok {key} task.done {task}[ receipt={receipt}][ artifact={artifact}] seq={seq}\nnext: {next}"
    f = {"key": "DEMO-0043", "task": "T3", "seq": 18, "receipt": "exit0/41000ms", "artifact": "tests.log", "next": "T4"}
    assert render.fill(t, f) == "ok DEMO-0043 task.done T3 receipt=exit0/41000ms artifact=tests.log seq=18\nnext: T4"
    del f["artifact"], f["next"]
    assert render.fill(t, f) == "ok DEMO-0043 task.done T3 receipt=exit0/41000ms seq=18"


def test_fill_missing_required_field_is_an_error():
    with pytest.raises(render.TemplateError):
        render.fill("ok {key} x {y}", {"key": "A-0001"})


def test_fill_keeps_the_first_line_one_line_and_formats_values():
    assert render.fill("ok {a} {b} {c}", {"a": "x\ny  z", "b": True, "c": ["p", "q"]}) == "ok x y z true p,q"
    assert render.fill("ok {n}", {"n": 0}) == "ok 0"


def test_result_text_lines_and_duplicate():
    op = ops.get("task.done")
    res = Result(data={"task": "T3"}, key="DEMO-0043", seq=18, hints=["T4"], lines=["extra"])
    assert render.result_text(op, res) == "ok DEMO-0043 task.done T3 seq=18\nextra\nnext: T4"
    res.duplicate = True
    assert render.result_text(op, res).splitlines()[0] == "ok DEMO-0043 task.done T3 seq=18 duplicate"


def test_result_envelope_matches_the_cli_result_schema():
    res = Result(data={"task": "T3"}, key="DEMO-0043", seq=18, cursor=20, hints=["T4"], lines=["not in json"])
    env = render.result_envelope(res)
    schema.validate("cli-result", env)
    assert env == {
        "v": "orch.cli/2.0",
        "ok": True,
        "data": {"task": "T3"},
        "key": "DEMO-0043",
        "seq": 18,
        "cursor": 20,
        "hints": ["T4"],
    }
    assert render.dumps(env) == (
        '{"v":"orch.cli/2.0","ok":true,"data":{"task":"T3"},"key":"DEMO-0043","seq":18,"cursor":20,"hints":["T4"]}'
    )
    res.duplicate = True
    env = render.result_envelope(res)
    schema.validate("cli-result", env)
    assert env["duplicate"] is True and list(env)[-1] == "duplicate"
    schema.validate("cli-result", render.result_envelope(Result()))


def test_json_output_has_no_spaces_and_keeps_unicode():
    assert render.dumps({"a": ["é", 1]}) == '{"a":["é",1]}'


def test_error_envelope_precedence_and_schema():
    op = ops.get("task.done")
    plain = render.error_envelope(OrchError("claim.required"), op)
    schema.validate("cli-error", plain)
    assert plain["error"]["fix"] == {"argv": ["orch", "claim"]}
    assert plain["error"]["retryable"] is False
    explicit = render.error_envelope(
        OrchError("claim.required", "no claim on DEMO-0043", hint="h", fix=["orch", "x"]), op
    )
    assert explicit["error"] == {
        "code": "claim.required",
        "message": "no claim on DEMO-0043",
        "hint": "h",
        "fix": {"argv": ["orch", "x"]},
        "retryable": False,
    }
    retry = render.error_envelope(OrchError("lock.busy"), op)
    assert retry["error"]["retryable"] is True
    no_op = render.error_envelope(OrchError("usage", "bad"))
    assert no_op["error"]["hint"] == "orch describe describe"
    unknown = render.error_envelope(OrchError("made.up"))
    assert unknown["error"] == {"code": "made.up", "message": "made.up", "retryable": False}
    schema.validate("cli-error", unknown)


def test_error_text_format():
    env = render.error_envelope(OrchError("human_only", "approve: human only"), ops.get("approve"))
    assert render.error_text(env) == (
        "err human_only approve: human only · retry:false · next: orch ask or orch wait · fix: orch describe ask"
    )
    env = render.error_envelope(OrchError("usage", "orch task: bad"), ops.get("task.done"))
    assert render.error_text(env) == "err usage orch task: bad · retry:false · next: orch describe task.done"
    assert render.error_text(env, color=True).startswith("\x1b[1;31merr\x1b[0m usage")


def test_color_only_on_a_terminal_and_never_with_json_or_no_color():
    class Tty(io.StringIO):
        def isatty(self):
            return True

    assert render.use_color(Tty(), False, {}) is True
    assert render.use_color(Tty(), True, {}) is False
    assert render.use_color(Tty(), False, {"NO_COLOR": "1"}) is False
    assert render.use_color(io.StringIO(), False, {}) is False


def test_redact_removes_secrets():
    assert render.redact("a SECRETSECRET b", ["SECRETSECRET"]) == "a [redacted] b"
    assert render.redact("short x", ["x"]) == "short x"
    assert render.redact("keep", [""]) == "keep"


def test_wait_result_examples_validate_and_exit_codes_are_the_results():
    schema.validate("wait-result", {"kind": "timeout", "key": "DEMO-0043", "cursor": 4, "next": "orch wait"})
    json.dumps(render.result_envelope(Result(data={"kind": "timeout"})))
