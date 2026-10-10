"""orch approve: approve a gate"""

from typing import Any

from orch.ops._dsl import REF_PATTERN, STR, E, S, err, obj, operation
from orch.ops.base import Context, Result
from orch.ops.human import Human, source_sha


def handle(ctx: Context, args: dict[str, Any]) -> Result:
    h = Human(ctx, "approve")
    gate = args["gate"]
    view = h.ticket(args.get("ref"))
    if gate == "code":  # D59: the code gate binds the source list like a verdict does (D58)
        view = h.observe(view)
    event: dict[str, Any] = {"type": "gate.approved", "gate": gate, **h.gate_basis(view, gate)}
    check = None
    if gate == "code":
        event["source_sha"] = source_sha(view)
        check = h.recheck_source(view, event["source_sha"])
    done = h.run(event, view.uid, f"approve {gate} of {view.key}", before_append=check)
    return h.ticket_result(view, done, {"gate": gate}, f"orch show {view.key}")


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
    errors=(
        err("gate.stale"),
        err("role.denied"),
        err("transition.refused"),
        err("source.missing"),
        err("not_found"),
        err("observe.unavailable"),
    ),
    handler=handle,
)
