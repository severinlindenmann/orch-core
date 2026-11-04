"""Move a follow-up's due time or event deadline."""

from datetime import datetime, timedelta

from orch.ops import followup
from orch.ops._dsl import STR, S, err, obj, operation
from orch.ops.errors import OrchError
from orch.ops.human import Human


def handle(ctx, args):
    h = Human(ctx, "follow_up.reschedule")
    view, rec = followup.find(h.call, args["id"])
    try:
        dt = datetime.fromisoformat(args["when"])
        if dt.utcoffset() != timedelta(0) or dt.microsecond:
            raise ValueError
        when = dt.strftime("%Y-%m-%dT%H:%M:%SZ")
    except ValueError:
        raise OrchError("followup.denied", "when must be an ISO UTC instant in whole seconds") from None
    field = "due" if rec["trigger"] == "time" else "deadline"
    event = {"type": "followup.rescheduled", "followup": rec["id"], field: when}
    action = f"move the follow-up {field} of {view.key} to {when}"
    done = h.run(event, view.uid, action, review=h.header(view, action))
    return h.ticket_result(view, done, {"id": rec["id"], field: when}, f"orch follow-up show {rec['id']}")


OP = operation(
    "follow_up.reschedule",
    "Human only",
    "Move a follow-up's due time or event deadline.",
    who="human",
    props={"id": S("follow-up ID"), "when": S("new time as an ISO UTC instant")},
    required=("id", "when"),
    positional=("id",),
    emits=("followup.rescheduled",),
    text="ok {key} followup.rescheduled {id} seq={seq}\nnext: {next}",
    data=obj({"id": STR, "due": STR, "deadline": STR}, optional=("due", "deadline")),
    errors=tuple(err(c) for c in ("followup.denied", "not_found")),
    handler=handle,
    addon="schedules",
)
