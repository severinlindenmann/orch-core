"""orch verdict: give the verify verdict"""

from orch.ops._dsl import MSG, REF_PATTERN, STR, E, S, err, obj, operation

OP = operation(
    "verdict",
    "Human only",
    "Give the verify verdict: pass, or fail with a reason.",
    who="human",
    props={
        "outcome": E("outcome", "pass", "fail", **{"x-metavar": "OUTCOME"}),
        "message": MSG,
        "ref": S("ticket REF (flag)", pattern=REF_PATTERN, **{"x-metavar": "REF"}),
    },
    required=("outcome",),
    positional=("outcome",),
    pre=("ticket_exists", "ticket_in_testing", "gate_current", "approver_eligible", "user_presence", "text_clean"),
    emits=("verdict.given",),
    text="ok {key} verdict.given {outcome} seq={seq}\nnext: {next}",
    data=obj({"outcome": STR}),
    errors=(
        err("gate.stale"),
        err("role.denied"),
        err("transition.refused"),
        err("source.missing"),
        err("parse.text"),
        err("not_found"),
    ),
)
