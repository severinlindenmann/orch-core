"""orch verdict: give the verify verdict"""

from typing import Any

from orch.ops._dsl import MSG, REF_PATTERN, STR, E, S, err, obj, operation
from orch.ops.base import Context, Result
from orch.ops.human import Human, source_sha


def handle(ctx: Context, args: dict[str, Any]) -> Result:
    h = Human(ctx, "verdict")
    outcome = args["outcome"]
    view = h.ticket(args.get("ref"))
    text = h.text(args, required=outcome == "fail", what="the reason")
    # D58: git is read right before the prompt; the verdict binds the commits it finds, a dirty or unobservable
    # repository is refused, and git is read again after the passphrase (the append is refused if the code moved)
    view = h.observe(view)
    event: dict[str, Any] = {
        "type": "verdict.given",
        "outcome": outcome,
        **h.gate_basis(view, "verify"),
        "source_sha": source_sha(view),
    }
    if text is not None:
        event["text"] = text
    done = h.run(
        event, view.uid, f"verdict {outcome} on {view.key}", before_append=h.recheck_source(view, event["source_sha"])
    )
    return h.ticket_result(view, done, {"outcome": outcome}, f"orch show {view.key}")


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
        err("observe.unavailable"),
    ),
    handler=handle,
)
