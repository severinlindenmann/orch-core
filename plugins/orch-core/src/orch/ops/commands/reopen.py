"""orch reopen: reopen a ticket"""

from typing import Any

from orch.ops._dsl import MSG, REF, err, obj, operation
from orch.ops.base import Context, Result
from orch.ops.human import Human


def handle(ctx: Context, args: dict[str, Any]) -> Result:
    h = Human(ctx, "reopen")
    view = h.ticket(args.get("ref"))
    event: dict[str, Any] = {"type": "ticket.reopened"}
    text = h.text(args, what="the reason")
    if text is not None:
        event["text"] = text
    done = h.run(event, view.uid, f"reopen {view.key}")
    return h.ticket_result(view, done, {}, f"orch show {view.key}")


OP = operation(
    "reopen",
    "Human only",
    "Reopen a done or closed ticket; raises every gate's generation.",
    who="human",
    props={"ref": REF("ticket REF"), "message": MSG},
    positional=("ref",),
    pre=("ticket_exists", "ticket_owner_or_maintainer", "user_presence", "text_clean"),
    emits=("ticket.reopened",),
    text="ok {key} ticket.reopened seq={seq}\nnext: {next}",
    data=obj({}),
    errors=(err("role.denied"), err("transition.refused"), err("parse.text"), err("not_found")),
    handler=handle,
)
