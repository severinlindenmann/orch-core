"""The ``needs`` language (ticket-format §8.1): validation, evaluation, totality and determinism."""

from __future__ import annotations

import json

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from orch.addons.needs_rules import MAX_DEPTH, MAX_NODES, NeedsRuleError, evaluate, validate_expr

ENV = {
    "status": "testing",
    "type": "feature",
    "size": "m",
    "priority": "high",
    "labels": ["db", "urgent"],
    "open_questions": 2,
    "tasks_open": 0,
    "acceptance": 3,
    "blocked": False,
    "gates": {"requirements": True, "plan": True, "verify": False, "code": False},
}
FIELDS = {"points": 5, "mood": "good", "tags": ["a"], "none": None}


def fires(expr, env=ENV, fields=FIELDS):
    validate_expr(expr, set(FIELDS))
    return evaluate(expr, env, fields)


def test_literals_and_variables():
    assert fires(["eq", ["var", "status"], "testing"])
    assert not fires(["eq", ["var", "status"], "open"])
    assert fires(["ne", ["var", "priority"], "low"])
    assert fires(["gt", ["var", "open_questions"], 1]) and fires(["le", ["var", "acceptance"], 3])
    assert fires(["not", ["var", "blocked"]])
    assert fires(["has", ["var", "labels"], "db"]) and not fires(["has", ["var", "labels"], "x"])
    assert fires(["in", ["var", "type"], "bug", "feature"]) and not fires(["in", ["var", "type"], "bug"])
    assert fires(["gate", "plan"]) and not fires(["gate", "verify"])
    assert fires(["gt", ["field", "points"], 3]) and fires(["eq", ["field", "mood"], "good"])
    assert fires(["is_null", ["field", "none"]]) and fires(["is_null", ["field", "points"]]) is False
    assert fires(["and", ["gate", "plan"], ["not", ["gate", "verify"]], ["lt", ["var", "tasks_open"], 1]])
    assert fires(["or", False, ["eq", 1, 1]])
    assert fires(["eq", ["var", "labels"], ["has", 1, 2]]) is False


def test_the_result_must_be_exactly_true():
    assert not evaluate("testing", ENV)  # a literal is not a rule that fires
    assert not evaluate(1, ENV) and not evaluate(None, ENV) and not evaluate(["var", "status"], ENV)
    assert evaluate(True, ENV)


def test_wrong_types_make_an_operator_false_not_an_error():
    assert not fires(["lt", ["var", "status"], 3])
    assert not fires(["gt", ["field", "mood"], 1])
    assert not fires(["has", ["var", "status"], "t"])
    assert not fires(["eq", True, 1]) and not fires(["eq", 1, True])  # True is not 1
    assert fires(["not", 5])  # a non-boolean operand is false
    assert not fires(["and", 1, True]) and not fires(["or", "x", 0])
    assert not fires(["lt", True, 2])  # booleans are not integers


def test_a_missing_or_mistyped_input_reads_as_null():
    assert evaluate(["is_null", ["var", "size"]], {})
    assert evaluate(["is_null", ["var", "open_questions"]], {"open_questions": "3"})  # wrong type
    assert evaluate(["is_null", ["field", "points"]], ENV, None)
    assert not evaluate(["gate", "plan"], {})
    assert evaluate(["is_null", ["field", "x"]], ENV, {"x": {"nested": 1}})  # an object is no value of the language


@pytest.mark.parametrize(
    "bad",
    [
        [],
        ["unknown", 1],
        ["var"],
        ["var", "nope"],
        ["var", ["var", "status"]],  # names are literal
        ["gate", "deploy"],
        ["field", "missing"],
        ["not"],
        ["not", 1, 2],
        ["eq", 1],
        ["and"],
        ["in", 1],
        ["has", 1],
        ["is_null"],
        [1, 2],
        {"op": "eq"},
        1.5,
        "x" * 201,
        2**53,
        ["eq", [], 1],
    ],
)
def test_malformed_expressions_are_refused_at_load(bad):
    with pytest.raises(NeedsRuleError):
        validate_expr(bad, set(FIELDS))
    assert evaluate(bad, ENV, FIELDS) in (True, False)  # and never raise when evaluated anyway


def test_depth_and_size_limits():
    e = True
    for _ in range(MAX_DEPTH - 1):
        e = ["not", e]
    validate_expr(e)
    with pytest.raises(NeedsRuleError, match="deeper"):
        validate_expr(["not", e])
    wide = ["or", *[True] * (MAX_NODES - 1)]
    validate_expr(wide)
    with pytest.raises(NeedsRuleError, match="nodes"):
        validate_expr(["or", *[True] * MAX_NODES])
    deep = True
    for _ in range(50_000):  # evaluation of something never loaded: bounded and total
        deep = ["not", deep]
    assert evaluate(deep, ENV) is False


def test_evaluation_does_not_change_its_inputs():
    env, fields = json.loads(json.dumps(ENV)), json.loads(json.dumps(FIELDS))
    evaluate(["has", ["var", "labels"], "db"], env, fields)
    assert env == ENV and fields == FIELDS


# ------------------------------------------------------------------------------------------------ property tests

JSON = st.recursive(
    st.none() | st.booleans() | st.integers(-(2**60), 2**60) | st.text(max_size=12) | st.floats(allow_nan=True),
    lambda c: st.lists(c, max_size=5) | st.dictionaries(st.text(max_size=5), c, max_size=3),
    max_leaves=40,
)
OPS = ["and", "or", "not", "eq", "ne", "lt", "le", "gt", "ge", "in", "has", "is_null", "var", "gate", "field", "zz"]
FORMS = st.recursive(
    st.none() | st.booleans() | st.integers(-5, 5) | st.sampled_from(["testing", "db", "x", "high"]),
    lambda c: st.builds(
        lambda op, args: [op, *args],
        st.sampled_from(OPS),
        st.lists(c | st.sampled_from(["status", "labels", "plan", "points", "open_questions"]), max_size=4),
    ),
    max_leaves=25,
)
ENVS = st.fixed_dictionaries(
    {},
    optional={
        "status": st.text(max_size=8) | st.none() | st.integers(),
        "labels": st.lists(st.text(max_size=3), max_size=3) | st.none() | st.text(),
        "open_questions": st.integers(-3, 3) | st.none() | st.text(),
        "blocked": st.booleans() | st.none() | st.integers(),
        "gates": st.dictionaries(st.sampled_from(["plan", "code", "x"]), st.booleans() | st.integers()) | st.none(),
    },
)


@settings(max_examples=300, deadline=None)
@given(expr=JSON, env=ENVS, fields=st.dictionaries(st.text(max_size=5), JSON, max_size=3))
def test_total_on_arbitrary_json(expr, env, fields):
    """Whatever the manifest, the ticket or the fields hold, evaluation returns a bool and does not raise."""
    out = evaluate(expr, env, fields)
    assert out is True or out is False


@settings(max_examples=300, deadline=None)
@given(expr=FORMS, env=ENVS, fields=st.dictionaries(st.sampled_from(["points", "mood"]), JSON, max_size=2))
def test_total_and_deterministic_on_expression_shaped_input(expr, env, fields):
    first = evaluate(expr, env, fields)
    assert first is True or first is False
    assert evaluate(expr, env, fields) == first  # the same inputs, the same answer, every time


@settings(max_examples=200, deadline=None)
@given(expr=FORMS, env=ENVS)
def test_what_validates_evaluates_without_a_limit_failure_in_the_shape(expr, env):
    try:
        validate_expr(expr, {"points", "mood"})
    except NeedsRuleError:
        return
    assert evaluate(expr, env, {"points": 1}) in (True, False)
