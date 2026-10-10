"""orch reopen: reopen a ticket"""

from orch.ops._dsl import MSG, REF, err, obj, operation

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
)
