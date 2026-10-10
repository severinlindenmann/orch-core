"""orch request-changes: ask for changes on a gate"""

from orch.ops._dsl import MSG, REF_PATTERN, STR, E, S, err, obj, operation

OP = operation(
    "request_changes",
    "Human only",
    "Request changes on a gate, with a reason.",
    who="human",
    props={
        "gate": E("gate", "requirements", "plan", "code", **{"x-metavar": "GATE"}),
        "message": MSG,
        "ref": S("ticket REF (flag)", pattern=REF_PATTERN, **{"x-metavar": "REF"}),
    },
    required=("gate", "message"),
    positional=("gate",),
    pre=("ticket_exists", "gate_current", "approver_eligible", "user_presence", "text_clean"),
    emits=("gate.changes_requested",),
    text="ok {key} gate.changes_requested {gate} seq={seq}\nnext: {next}",
    data=obj({"gate": STR}),
    errors=(err("gate.stale"), err("role.denied"), err("transition.refused"), err("parse.text"), err("not_found")),
)
