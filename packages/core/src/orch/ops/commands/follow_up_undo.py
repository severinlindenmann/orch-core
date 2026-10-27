"""Undo a recent person cancellation of a follow-up."""

from orch.ops import followup
from orch.ops._dsl import STR, S, err, obj, operation
from orch.ops.human import Human


def handle(ctx, args):
    h = Human(ctx, "follow_up.undo")
    view, rec = followup.find(h.call, args["id"])
    event = {"type": "followup.restored", "followup": rec["id"]}
    action = f"restore the cancelled follow-up of {view.key}"
    done = h.run(event, view.uid, action, review=h.header(view, action))
    return h.ticket_result(view, done, {"id": rec["id"]}, f"orch follow-up show {rec['id']}")


OP = operation(
    "follow_up.undo",
    "Human only",
    "Undo your follow-up cancellation within 120 seconds.",
    who="human",
    props={"id": S("follow-up ID")},
    required=("id",),
    positional=("id",),
    emits=("followup.restored",),
    text="ok {key} followup.restored {id} seq={seq}\nnext: {next}",
    data=obj({"id": STR}),
    errors=tuple(err(c) for c in ("followup.denied", "not_found")),
    handler=handle,
    addon="schedules",
)
