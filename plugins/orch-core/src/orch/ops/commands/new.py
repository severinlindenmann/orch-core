"""orch new: create a ticket"""

from orch.ops._dsl import FILE, LABEL_PATTERN, MSG, STR, E, L, S, err, obj, operation

OP = operation(
    "new",
    "Lifecycle",
    "Create a ticket.",
    who="agent",
    props={
        "title": S("one line, at most 200 characters", maxLength=200, **{"x-metavar": "TITLE"}),
        "type": E("ticket type", "feature", "bug", "chore", "spike", "epic", default="feature"),
        "priority": E("priority", "low", "medium", "high", "urgent"),
        "size": E("size", "xs", "s", "m", "l", "xl"),
        "label": L(
            "labels, comma separated",
            split=True,
            items={"type": "string", "pattern": LABEL_PATTERN},
            **{"x-metavar": "A,B"},
        ),
        "parent": S("parent ticket key", **{"x-metavar": "KEY"}),
        "message": MSG,
        "file": FILE,
    },
    required=("title",),
    positional=("title",),
    pre=("workspace_exists", "grant_valid", "text_clean"),
    emits=("ticket.created",),
    text="ok {key} ticket.created {ticket_type} seq={seq}\nnext: {next}",
    data=obj({"title": STR, "ticket_type": STR}),
    errors=(err("not_found", "the parent does not exist", ["orch", "list"]), err("parse.text")),
)
