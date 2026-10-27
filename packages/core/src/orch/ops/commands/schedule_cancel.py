"""Atomic manual schedule cancellation; Undo requires a new schedule.set."""

from orch.ops import schedule
from orch.ops._dsl import STR, S, err, obj, operation
from orch.ops.human import Human


def handle(ctx, args):
    h = Human(ctx, "schedule.cancel")
    view, rec = schedule.find(h.call, args["id"])
    action = f"Cancel automatic sessions from schedule {rec['id']} on {view.key}"
    done = h.run({"type": "schedule.cancelled", "schedule": rec["id"]}, view.uid, action, review=h.header(view, action))
    return h.ticket_result(view, done, {"id": rec["id"]}, "orch schedule list")


OP = operation(
    "schedule.cancel",
    "Lifecycle",
    "Cancel a schedule and end its run grants.",
    who="human",
    props={"id": S("schedule ID")},
    required=("id",),
    positional=("id",),
    emits=("schedule.cancelled",),
    text="ok {key} schedule.cancelled {id} seq={seq}\nnext: {next}",
    data=obj({"id": STR}),
    errors=tuple(err(c) for c in ("schedule.denied", "not_found")),
    handler=handle,
    addon="schedules",
)
