from __future__ import annotations

import json

import pytest

import orch.ops as ops
from orch import schema
from orch.cli.main import Hooks
from orch.cli.session import DEDUP_SECONDS, STOP_SECONDS, FileRecords, MemoryRecords
from orch.ops import Result
from orch.ops.errors import ERRORS, EXIT_CODES, OrchError
from tests.cli.conftest import GRANT, SESSION

SESSION_ENV = {"ORCH_SESSION": SESSION}
AGENT_ENV = {"ORCH_SESSION": SESSION, "ORCH_GRANT": GRANT}


def test_exit_codes_are_the_table_of_10_4():
    assert EXIT_CODES == {
        0: "ok",
        1: "internal error",
        2: "usage or not found",
        3: "not allowed (transition or human-only)",
        4: "claim, lease or lock",
        5: "validation",
        6: "parse",
        7: "wait timeout with --strict-timeout",
        8: "base_rev conflict",
        9: "retryable",
    }
    by_code = {c: s.exit for c, s in ERRORS.items()}
    assert by_code["internal"] == 1 and by_code["usage"] == 2 and by_code["not_found"] == 2
    assert by_code["human_only"] == 3 and by_code["transition.refused"] == 3
    assert by_code["claim.held"] == 4 and by_code["lease.held"] == 4 and by_code["lock.busy"] == 4
    assert by_code["invalid.input"] == 5 and by_code["parse.json"] == 6 and by_code["wait.timeout"] == 7
    assert by_code["conflict.section"] == 8 and by_code["quota.unattended"] == 9


def test_text_error_goes_to_stderr_with_the_exit_code(cli):
    r = cli("approve", "plan")
    assert (r.code, r.out) == (3, "")
    assert (
        r.err
        == "err human_only approve: human only · retry:false · next: orch ask or orch wait · fix: orch describe ask\n"
    )


def test_json_error_goes_to_stdout_as_the_envelope(cli):
    r = cli("approve", "plan", "--json")
    assert (r.code, r.err) == (3, "")
    env = json.loads(r.out)
    schema.validate("cli-error", env)
    assert env == {
        "ok": False,
        "error": {
            "code": "human_only",
            "message": "approve: human only",
            "hint": "orch ask or orch wait",
            "fix": {"argv": ["orch", "describe", "ask"]},
            "retryable": False,
        },
    }


def test_orch_output_env_selects_json(cli):
    r = cli("approve", "plan", env={"ORCH_OUTPUT": "json"})
    assert json.loads(r.out)["error"]["code"] == "human_only"


def test_every_human_operation_is_refused_to_an_agent(cli):
    for op in ops.all():
        if op.who != "human":
            continue
        r = cli(*op.words, *_minimal_args(op), "--json", env=AGENT_ENV)
        assert (r.code, json.loads(r.out)["error"]["code"]) == (3, "human_only"), op.name


def _minimal_args(op) -> list[str]:
    sample = {
        "gate": "plan", "outcome": "pass", "question": "Q1", "grant": GRANT.split(".")[0], "person": "p_" + "a" * 32,
        "role": "member", "name": "x", "prefix": "DEMO", "path": "/x", "ref": "1", "pk": "k", "cert": "-",
        "message": "m",
    }  # fmt: skip
    schema_in = op.input
    out: list[str] = []
    for name in schema_in.get("x-positional", []):
        if name in schema_in.get("required", []):
            out.append(sample[name])
    for name in schema_in.get("required", []):
        if name not in schema_in.get("x-positional", []):
            out += ["--" + name.replace("_", "-"), sample[name]]
    return out


def test_agent_operations_need_a_grant(cli):
    r = cli("claim", "--next", "--json")
    assert (r.code, json.loads(r.out)["error"]["code"]) == (3, "grant.required")
    r = cli("claim", "--next", "--json", env={"ORCH_GRANT": "nonsense"})
    assert json.loads(r.out)["error"] == {
        **json.loads(r.out)["error"],
        "code": "grant.required",
        "message": "ORCH_GRANT is malformed",
    }
    r = cli("claim", "--next", env={"ORCH_GRANT": GRANT})
    assert (r.code, "not_implemented") == (1, "not_implemented") and "err not_implemented" in r.err


def test_unattended_and_read_operations_work_without_a_grant(cli):
    for argv in (["ask", "q?"], ["log", "n"], ["artifact", "add", "/x"], ["status"], ["show"]):
        r = cli(*argv)
        assert "not_implemented" in r.err, argv


def test_usage_errors_exit_2_and_point_at_describe(cli):
    r = cli("task", "done", "--json")
    env = json.loads(r.out)
    assert r.code == 2 and env["error"]["code"] == "usage"
    assert env["error"]["fix"] == {"argv": ["orch", "describe", "task.done"]}
    assert cli("frobnicate").code == 2
    assert json.loads(cli("frobnicate", "--json").out)["error"]["code"] == "unknown_command"


def test_input_is_validated_against_the_schema(cli):
    r = cli("task", "done", "banana", "--json", env=AGENT_ENV)
    env = json.loads(r.out)
    assert r.code == 5 and env["error"]["code"] == "invalid.input"
    assert env["error"]["message"] == "task done: task: has the wrong form"
    r = cli("wait", "--timeout", "0", "--json")
    assert json.loads(r.out)["error"]["message"] == "wait: timeout: fails minimum 1"


def test_bad_session_is_refused(cli):
    r = cli("status", "--json", env={"ORCH_SESSION": "nope"})
    assert r.code == 5 and json.loads(r.out)["error"]["code"] == "invalid.input"


def test_handler_error_codes_must_be_declared(cli, bound):
    def handler(ctx, args):
        raise OrchError("claim.held")

    bound("log", handler)
    r = cli("log", "x", "--json")
    env = json.loads(r.out)
    assert r.code == 1 and env["error"]["code"] == "internal" and "undeclared" in env["error"]["message"]


def test_declared_handler_error_uses_the_declared_hint_and_exit(cli, bound):
    def handler(ctx, args):
        raise OrchError("quota.unattended")

    bound("log", handler)
    r = cli("log", "x", "--json")
    env = json.loads(r.out)
    assert (r.code, env["error"]["code"], env["error"]["retryable"]) == (9, "quota.unattended", True)


def test_unexpected_exception_becomes_an_internal_envelope(cli, bound):
    def handler(ctx, args):
        raise RuntimeError("boom")

    bound("log", handler)
    r = cli("log", "x", "--json")
    assert r.code == 1 and json.loads(r.out)["error"]["message"] == "RuntimeError: boom"


def test_a_bad_template_is_an_internal_error(cli, bound):
    bound("log", lambda ctx, args: Result(key=None))
    r = cli("log", "x")
    assert r.code == 1 and "err internal output template" in r.err


# ---- stop rule (advisory)


def refuse_with(bound, code="claim.required", op="task.start"):
    def handler(ctx, args):
        raise OrchError(code)

    bound(op, handler)


def test_stop_rule_third_identical_refusal_returns_stop(cli):
    rec = MemoryRecords()
    codes = []
    for _ in range(4):
        r = cli("approve", "plan", "--json", env=SESSION_ENV, records=rec)
        codes.append((r.code, json.loads(r.out)["error"]["code"]))
    assert codes == [(3, "human_only"), (3, "human_only"), (3, "stop"), (3, "stop")]
    r = cli("approve", "plan", env=SESSION_ENV, records=rec)
    assert r.err.startswith("err stop STOP: report to the user · retry:false")


def test_human_only_counts_by_operation_ignoring_arguments(cli):
    rec = MemoryRecords()
    got = [cli("approve", g, "--json", env=SESSION_ENV, records=rec) for g in ("plan", "code", "requirements")]
    assert [json.loads(r.out)["error"]["code"] for r in got] == ["human_only", "human_only", "stop"]
    assert json.loads(cli("approve", "plan", "--json", env=SESSION_ENV, records=rec).out)["error"]["code"] == "stop"
    assert json.loads(cli("close", "1", "--json", env=SESSION_ENV, records=rec).out)["error"]["code"] == "human_only"


def test_other_refusals_count_by_arguments_and_normalised_ref(cli, bound):
    refuse_with(bound)
    rec = MemoryRecords()
    hooks = Hooks(normalise_ref=lambda ref: ref.lstrip("0") if ref.isdigit() else ref.split("-")[-1].lstrip("0"))

    def go(*argv):
        return json.loads(cli(*argv, "--json", env=AGENT_ENV, records=rec, hooks=hooks).out)["error"]["code"]

    assert [go("task", "start", "DEMO-0043/T3")] == ["claim.required"]
    assert go("task", "start", "T4") == "claim.required"  # another call: its own count
    assert go("task", "start", "T4") == "claim.required"
    assert go("task", "start", "T4") == "stop"
    # a REF in any spelling is the same call
    rec2 = MemoryRecords()
    refuse_with(bound, code="claim.held", op="claim")
    out = [
        json.loads(cli("claim", ref, "--json", env=AGENT_ENV, records=rec2, hooks=hooks).out)["error"]["code"]
        for ref in ("43", "DEMO-0043", "0043")
    ]
    assert out == ["claim.held", "claim.held", "stop"]


def test_stop_expires_after_15_minutes_and_window_slides(cli):
    rec = MemoryRecords()
    clock = [1000.0]

    def go():
        r = cli("approve", "plan", "--json", env=SESSION_ENV, records=rec, now=lambda: clock[0])
        return json.loads(r.out)["error"]["code"]

    assert [go(), go(), go()] == ["human_only", "human_only", "stop"]
    clock[0] += STOP_SECONDS + 1
    assert go() == "human_only"
    clock[0] += STOP_SECONDS - 10
    assert go() == "human_only"  # the earlier ones left the window


def test_stop_rule_resets_only_on_a_successful_write(cli, bound):
    bound("log", lambda ctx, args: Result(key="DEMO-0001", seq=2))
    bound("show", lambda ctx, args: Result(data={"view": "x"}, key="DEMO-0001", seq=2))
    rec = MemoryRecords()
    for _ in range(2):
        cli("approve", "plan", env=SESSION_ENV, records=rec)
    assert cli("show", env=SESSION_ENV, records=rec).code == 0  # a read does not reset
    assert cli("approve", "plan", "--json", env=SESSION_ENV, records=rec).code == 3
    assert json.loads(cli("approve", "plan", "--json", env=SESSION_ENV, records=rec).out)["error"]["code"] == "stop"
    # a dry run is not a write either
    cli("log", "x", "--dry-run", env=SESSION_ENV, records=rec)
    assert json.loads(cli("approve", "plan", "--json", env=SESSION_ENV, records=rec).out)["error"]["code"] == "stop"
    cli("log", "x", env=SESSION_ENV, records=rec)
    assert cli("approve", "plan", env=SESSION_ENV, records=rec).err.startswith("err human_only")


def test_usage_errors_do_not_count(cli):
    rec = MemoryRecords()
    for _ in range(5):
        assert json.loads(cli("task", "done", "--json", env=SESSION_ENV, records=rec).out)["error"]["code"] == "usage"


def test_stop_rule_is_per_session_and_needs_one(cli):
    rec = MemoryRecords()
    other = {"ORCH_SESSION": "s_01J9ZK4Q7M3R8T2V6X0B5N1C9E"}
    for _ in range(2):
        cli("approve", "plan", env=SESSION_ENV, records=rec)
    assert cli("approve", "plan", env=other, records=rec).err.startswith("err human_only")
    for _ in range(4):
        assert cli("approve", "plan", records=rec).err.startswith("err human_only")


def test_retryable_and_internal_errors_do_not_count_toward_stop(cli):
    rec = MemoryRecords()
    for _ in range(4):
        r = cli("claim", "--next", env={**AGENT_ENV}, records=rec)
        assert r.err.startswith("err not_implemented")


def test_file_records_persist_between_processes(cli, tmp_path):
    for _ in range(2):
        cli("approve", "plan", env=SESSION_ENV, records=FileRecords(tmp_path / "session"))
    r = cli("approve", "plan", "--json", env=SESSION_ENV, records=FileRecords(tmp_path / "session"))
    assert json.loads(r.out)["error"]["code"] == "stop"
    assert (tmp_path / "session" / f"{SESSION}.json").exists()


def test_file_records_tolerate_a_file_with_missing_keys(cli, tmp_path):
    d = tmp_path / "session"
    d.mkdir()
    (d / f"{SESSION}.json").write_text("{}")
    assert cli("approve", "plan", env=SESSION_ENV, records=FileRecords(d)).err.startswith("err human_only")


def _hammer(directory: str, session: str, tag: str, n: int) -> None:
    from orch.cli.session import FileRecords

    rec = FileRecords(directory)
    for i in range(n):
        rec.refused(session, "approve", {"i": f"{tag}{i}"}, "human_only", 1000.0)
        rec.remember(session, f"{tag}{i}", 1000.0, {"i": i})


def test_file_records_two_writers_lose_no_update_and_corruption_fails_closed(cli, tmp_path):
    """Two processes of one session refuse and succeed concurrently and every record survives; a corrupt file is an
    error, never a reset (the dedup-with-append ordering is tested in tests/store/test_cli_wiring.py)."""
    import multiprocessing as mp

    d = tmp_path / "rec"
    ctx = mp.get_context("spawn")
    with ctx.Pool(2) as pool:
        rs = [pool.apply_async(_hammer, (str(d), SESSION, t, 15)) for t in ("a", "b")]
        [r.get(timeout=120) for r in rs]
    rec = FileRecords(d)
    for t in ("a", "b"):
        for i in range(15):
            assert rec.recall(SESSION, f"{t}{i}", 1000.0) == {"i": i}
    assert len(rec._get(SESSION)["refusals"]) == 1  # human_only counts by operation, whatever the arguments
    (d / f"{SESSION}.json").write_text("{ torn")
    r = cli("approve", "plan", "--json", env=SESSION_ENV, records=FileRecords(d))
    assert json.loads(r.out)["error"]["code"] == "internal" and (d / f"{SESSION}.json").read_text() == "{ torn"
    (d / f"{SESSION}.json").write_text("[1, 2]")
    assert json.loads(cli("approve", "plan", "--json", env=SESSION_ENV, records=FileRecords(d)).out)["error"]["code"] == "internal"


# ---- retry dedup


def test_retry_within_15_minutes_returns_the_original_with_duplicate(cli, bound):
    calls = []

    def handler(ctx, args):
        calls.append(args)
        return Result(data={}, key="DEMO-0043", seq=7 + len(calls), cursor=7)

    bound("log", handler)
    clock = [1000.0]
    rec = MemoryRecords()
    first = cli("log", "same", "--json", env=SESSION_ENV, records=rec, now=lambda: clock[0])
    clock[0] += DEDUP_SECONDS - 1
    again = cli("log", "same", "--json", env=SESSION_ENV, records=rec, now=lambda: clock[0])
    assert len(calls) == 1
    a, b = json.loads(first.out), json.loads(again.out)
    assert "duplicate" not in a and b["duplicate"] is True
    assert {k: v for k, v in b.items() if k != "duplicate"} == a
    schema.validate("cli-result", b)
    text = cli("log", "same", env=SESSION_ENV, records=rec, now=lambda: clock[0])
    assert text.out == "ok DEMO-0043 log.added seq=8 duplicate\n"


def test_retry_after_15_minutes_or_with_other_args_or_session_runs_again(cli, bound):
    calls = []
    bound("log", lambda ctx, args: calls.append(args) or Result(key="DEMO-0043", seq=len(calls)))
    clock = [1000.0]
    rec = MemoryRecords()

    def go(text, env=SESSION_ENV):
        return cli("log", text, env=env, records=rec, now=lambda: clock[0])

    go("a")
    go("b")
    go("a", {"ORCH_SESSION": "s_01J9ZK4Q7M3R8T2V6X0B5N1C9E"})
    assert len(calls) == 3
    clock[0] += DEDUP_SECONDS + 1
    go("a")
    assert len(calls) == 4


def test_dry_run_and_sessionless_calls_are_never_deduplicated(cli, bound):
    calls = []
    bound("log", lambda ctx, args: calls.append(ctx.dry_run) or Result(key="DEMO-0043", seq=1))
    rec = MemoryRecords()
    cli("log", "x", "--dry-run", env=SESSION_ENV, records=rec)
    cli("log", "x", "--dry-run", env=SESSION_ENV, records=rec)
    cli("log", "x", records=rec)
    cli("log", "x", records=rec)
    assert calls == [True, True, False, False]


def test_reads_are_not_deduplicated(cli, bound):
    calls = []
    bound("show", lambda ctx, args: calls.append(1) or Result(data={"view": "default"}, key="DEMO-0043", seq=1))
    rec = MemoryRecords()
    cli("show", env=SESSION_ENV, records=rec)
    cli("show", env=SESSION_ENV, records=rec)
    assert len(calls) == 2


def test_failed_calls_are_not_remembered(cli, bound):
    state = {"n": 0}

    def handler(ctx, args):
        state["n"] += 1
        if state["n"] == 1:
            raise OrchError("quota.unattended")
        return Result(key="DEMO-0043", seq=1)

    bound("log", handler)
    rec = MemoryRecords()
    assert cli("log", "x", env=SESSION_ENV, records=rec).code == 9
    ok = cli("log", "x", "--json", env=SESSION_ENV, records=rec)
    assert ok.code == 0 and "duplicate" not in json.loads(ok.out)


def test_the_grant_secret_never_reaches_output(cli, bound):
    secret = GRANT.split(".")[1]
    bound("log", lambda ctx, args: Result(data={"echo": ctx.grant}, key="DEMO-0043", seq=1, lines=[f"leak {secret}"]))
    r = cli("log", "x", "--json", env=AGENT_ENV)
    assert secret not in r.out and GRANT not in r.out and "[redacted]" in r.out
    r = cli("log", "x", env=AGENT_ENV)
    assert secret not in r.out


def test_wait_style_exit_code_comes_from_the_result(cli, bound):
    bound("wait", lambda ctx, args: Result(data={"kind": "changes_requested"}, key="DEMO-0043", exit=3))
    r = cli("wait", "--json")
    assert r.code == 3 and json.loads(r.out)["ok"] is True


@pytest.mark.parametrize("code", sorted(ERRORS))
def test_every_catalog_error_renders_a_valid_envelope(code):
    from orch.cli import render

    schema.validate("cli-error", render.error_envelope(OrchError(code)))
