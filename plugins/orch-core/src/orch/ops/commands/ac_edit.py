"""orch ac edit: change an acceptance criterion"""

from orch.ops._dsl import REF_PATTERN, STR, S, err, obj, operation

OP = operation(
    "ac.edit",
    "Edit",
    "Change the text of an acceptance criterion.",
    who="agent",
    props={
        "ac": S("criterion id", pattern=r"^AC[1-9][0-9]*$", **{"x-metavar": "AC"}),
        "text": S("the new text", **{"x-metavar": "TEXT"}),
        "ref": S("ticket REF (flag)", pattern=REF_PATTERN, **{"x-metavar": "REF"}),
    },
    required=("ac", "text"),
    positional=("ac", "text"),
    pre=("ticket_exists", "ticket_open_for_work", "acceptance_exists", "base_rev_tracked", "grant_valid", "text_clean"),
    emits=("ticket.updated",),
    text="ok {key} ticket.updated {ac} seq={seq}\nnext: {next}",
    data=obj({"ac": STR}),
    errors=(err("conflict.field"), err("transition.refused"), err("not_found"), err("parse.text")),
)
