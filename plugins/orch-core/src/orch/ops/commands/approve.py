"""orch approve: approve a gate"""

from orch.ops._dsl import REF_PATTERN, STR, E, S, err, obj, operation

OP = operation(
    "approve",
    "Human only",
    "Approve a gate (requirements, plan or code) with your signature.",
    who="human",
    props={
        "gate": E("gate", "requirements", "plan", "code", **{"x-metavar": "GATE"}),
        "ref": S("ticket REF (flag)", pattern=REF_PATTERN, **{"x-metavar": "REF"}),
    },
    required=("gate",),
    positional=("gate",),
    pre=("ticket_exists", "gate_current", "approver_eligible", "user_presence"),
    emits=("gate.approved",),
    text="ok {key} gate.approved {gate} seq={seq}\nnext: {next}",
    data=obj({"gate": STR}),
    errors=(err("gate.stale"), err("role.denied"), err("transition.refused"), err("source.missing"), err("not_found")),
)
