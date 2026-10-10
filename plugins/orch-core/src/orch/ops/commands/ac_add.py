"""orch ac add: add an acceptance criterion"""

from typing import Any

from orch.ops import plans
from orch.ops._dsl import REF_PATTERN, STR, S, err, obj, operation
from orch.ops.base import Context, Result


def handle(ctx: Context, args: dict[str, Any]) -> Result:
    return plans.run(ctx, "ac.add", args, plans.ac_add)


OP = operation(
    "ac.add",
    "Edit",
    "Add an acceptance criterion.",
    who="agent",
    props={
        "text": S("the criterion", **{"x-metavar": "TEXT"}),
        "ref": S("ticket REF (flag)", pattern=REF_PATTERN, **{"x-metavar": "REF"}),
    },
    required=("text",),
    positional=("text",),
    pre=("ticket_exists", "ticket_open_for_work", "base_rev_tracked", "grant_valid", "text_clean"),
    emits=("ticket.updated",),
    text="ok {key} ticket.updated {ac} seq={seq}\nnext: {next}",
    data=obj({"ac": STR}),
    errors=(err("conflict.field"), err("transition.refused"), err("not_found"), err("parse.text")),
    handler=handle,
)
