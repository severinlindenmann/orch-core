"""orch close: close a ticket"""

from orch.ops._dsl import MSG, REF, STR, E, S, err, obj, operation

OP = operation(
    "close",
    "Human only",
    "Close a ticket without finishing it.",
    who="human",
    props={
        "ref": REF("ticket REF"),
        "resolution": E("why", "wont_do", "duplicate", "obsolete", "other", default="other"),
        "duplicate_of": S("the surviving ticket (with --resolution duplicate)", **{"x-metavar": "KEY"}),
        "message": MSG,
    },
    positional=("ref",),
    pre=("ticket_exists", "ticket_owner_or_maintainer", "user_presence", "text_clean"),
    emits=("ticket.closed",),
    text="ok {key} ticket.closed {resolution} seq={seq}\nnext: {next}",
    data=obj({"resolution": STR}),
    errors=(err("role.denied"), err("transition.refused"), err("parse.text"), err("not_found")),
)
