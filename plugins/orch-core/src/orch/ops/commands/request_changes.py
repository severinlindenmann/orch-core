"""orch request-changes: ask for changes on a gate"""

from typing import Any

from orch.ops._dsl import MSG, REF_PATTERN, STR, E, S, err, obj, operation
from orch.ops.base import Context, Result
from orch.ops.human import Human


def handle(ctx: Context, args: dict[str, Any]) -> Result:
    h = Human(ctx, "request_changes")
    gate = args["gate"]
    view = h.ticket(args.get("ref"))
    text = h.text(args, required=True, what="the reason")
    event = {"type": "gate.changes_requested", "gate": gate, **h.gate_basis(view, gate), "text": text}
    done = h.run(event, view.uid, f"request changes on {gate} of {view.key}")
    return h.ticket_result(view, done, {"gate": gate}, f"orch show {view.key}")


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
    handler=handle,
)
