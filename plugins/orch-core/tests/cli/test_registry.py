from __future__ import annotations

import pytest

import orch.ops as ops
from orch import schema
from orch.ops import Context, NotImplementedYet, Operation, _dsl
from orch.ops.actors import EVENT_ACTORS, allows
from orch.ops.errors import ERRORS, EXIT_CODES, GLOBAL_ERRORS
from orch.ops.preconditions import PRECONDITIONS
from orch.ops.workflows import WORKFLOWS

# Format doc 10.3, one registry operation per command (subcommands are separate operations).
SECTION_10_3 = [
    *"status describe help show list search next inbox new claim release handoff submit ask wait set".split(),
    "section set", "ac add", "ac edit",
    *(f"task {v}" for v in "list next add start done skip block reopen".split()),
    *(f"artifact {v}" for v in "add replace list".split()),
    "log", "apply", "approve", "request-changes", "verdict", "answer", "close", "reopen",
    "grant", "grant revoke", "member add", "member remove", "member role",
    "init", "doctor", "check", "instructions sync", "instructions hook", "import v1",
    *(f"addon {v}" for v in "list grant disable purge".split()),
]  # fmt: skip


def test_every_10_3_command_registered_exactly_once():
    got = [op.cli for op in ops.all()]
    assert sorted(got) == sorted(set(got)), "a command is registered twice"
    assert sorted(got) == sorted(SECTION_10_3)


def test_names_are_unique_and_match_cli_words():
    names = ops.names()
    assert len(names) == len(set(names))
    for op in ops.all():
        assert ops.get(op.name) is op
        assert ops.resolve(list(op.words)) is op
        assert ops.resolve([op.name]) is op


def test_sample_declarations_validate_against_the_operation_schema():
    for name in ("status", "task.done", "wait", "approve"):
        ops.get(name).validate()


@pytest.mark.slow
def test_every_declaration_validates_against_the_operation_schema():
    for op in ops.all():
        op.validate()


def test_register_refuses_duplicates():
    with pytest.raises(ValueError):
        ops.register(ops.get("status"))
    clone = Operation(
        declaration={**ops.get("status").declaration, "name": "status2"},
        group="Context",
        handler=ops.get("status").handler,
    )
    clone2 = Operation(
        declaration={**ops.get("status").declaration, "name": "status"},
        group="Context",
        handler=ops.get("status").handler,
    )
    del clone
    with pytest.raises(ValueError):
        ops.register(clone2)


def test_get_unknown_raises():
    with pytest.raises(KeyError):
        ops.get("nope")


def test_every_emitted_type_exists_in_f1_5_4():
    known = set(schema.event_types())
    assert known == set(EVENT_ACTORS), "orch.ops.actors drifted from the schemas' event types"
    for op in ops.all():
        for t in op.emits:
            assert t in known, f"{op.name} emits unknown type {t}"
            assert f"event.{t}" in schema.names()


def test_who_matches_the_actor_rules_of_the_events_it_emits():
    for op in ops.all():
        if op.who == "read":
            assert op.emits == [], f"{op.name} is read but emits"
        for t in op.emits:
            assert allows(op.who, t), f"{op.name} (who={op.who}) cannot append {t} ({sorted(EVENT_ACTORS[t])})"


def test_an_agent_operation_cannot_emit_a_person_only_event():
    person_only = {t for t, a in EVENT_ACTORS.items() if a == frozenset("P")}
    for op in ops.all():
        if op.who in ("agent", "unattended"):
            assert not set(op.emits) & person_only, op.name
    assert not allows("agent", "gate.approved")
    assert not allows("unattended", "claim.taken")
    assert allows("human", "ticket.closed")
    assert not allows("human", "branch.pushed")


def test_writes_that_need_no_grant_are_the_unattended_three():
    assert {op.name for op in ops.all() if op.who == "unattended"} == {"ask", "log", "artifact.add"}


def test_human_operations_are_the_human_only_list():
    human = {op.name for op in ops.all() if op.who == "human"}
    for name in ("approve", "request_changes", "verdict", "answer", "close", "reopen", "grant", "member.add"):
        assert name in human


def test_error_codes_are_unique_documented_and_have_an_exit_code():
    assert len(ERRORS) == len({c for c in ERRORS})
    for code, spec in ERRORS.items():
        assert spec.code == code
        assert spec.exit in EXIT_CODES and spec.exit != 0
        assert spec.doc and spec.message and spec.hint and spec.fix and spec.fix[0] == "orch"
        schema.validate("operation", _min_op(code))
    for code in GLOBAL_ERRORS:
        assert code in ERRORS


def _min_op(code: str) -> dict:
    spec = ERRORS[code]
    return {
        "name": "x",
        "input": {"type": "object"},
        "who": "read",
        "pre": [],
        "emits": [],
        "output": {"text": "ok x", "json": {"type": "object"}},
        "errors": [{"code": code, "hint": spec.hint, "fix": {"argv": list(spec.fix)}}],
    }


def test_operation_errors_are_declared_once_known_and_complete():
    used: set[str] = set()
    for op in ops.all():
        codes = [e["code"] for e in op.errors]
        assert len(codes) == len(set(codes)), f"{op.name} repeats an error code"
        for e in op.errors:
            assert e["code"] in ERRORS, (op.name, e["code"])
            assert e["code"] not in GLOBAL_ERRORS
            assert e["hint"] and e["fix"]["argv"][0] == "orch"
            assert isinstance(e["retryable"], bool)
            used.add(e["code"])
        if op.who == "human":
            assert "human_only" in codes
        if op.who == "agent":
            assert "grant.required" in codes
        if op.is_write:
            assert "lock.busy" in codes
    unused = set(ERRORS) - used - set(GLOBAL_ERRORS) - {"wait.timeout"}
    assert not unused, f"documented but no operation can return them: {sorted(unused)}"


def test_preconditions_are_named_and_documented():
    for op in ops.all():
        for p in op.pre:
            assert p in PRECONDITIONS, (op.name, p)


def test_output_templates_are_ok_line_plus_next():
    for op in ops.all():
        text = op.output["text"]
        assert text.startswith("ok ")
        assert len(text.split("\n")) <= 2
        if "\n" in text:
            assert text.split("\n")[1].startswith("next: ")


def test_wait_uses_the_wait_result_schema():
    assert ops.get("wait").output["json"] == {"$ref": "https://schemas.orch.dev/v2/wait-result"}


def test_every_handler_raises_not_implemented_yet_except_the_meta_ones():
    implemented = {"describe", "help"}
    for op in ops.all():
        if op.name in implemented:
            continue
        with pytest.raises(NotImplementedYet) as e:
            op.handler(Context(), {})
        assert e.value.op == op.name


def test_bind_replaces_the_handler_and_keeps_the_declaration(bound):
    seen = []

    def handler(ctx, args):
        seen.append(args)
        return ops.Result()

    before = ops.get("log").declaration
    bound("log", handler)
    assert ops.get("log").declaration is before
    ops.get("log").handler(Context(), {"x": 1})
    assert seen == [{"x": 1}]


def test_workflows_name_registered_operations():
    for name, (_one, steps) in WORKFLOWS.items():
        assert steps, name
        for op_name, call in steps:
            assert ops.get(op_name)
            assert call.split("orch ", 1)[1].split()[0] == ops.get(op_name).words[0], (name, op_name, call)


def test_dsl_refuses_unknown_precondition_and_global_error():
    with pytest.raises(ValueError):
        _dsl.operation("zz", "Read", "s", who="read", text="ok zz", pre=("nope",))
    with pytest.raises(ValueError):
        _dsl.operation("zz", "Read", "s", who="read", text="ok zz", errors=(_dsl.err("usage"),))


def test_group_order_is_the_command_table():
    assert [g for g, _ in ops.iter_groups()] == ["Context", "Read", "Lifecycle", "Edit", "Human only", "Admin"]
