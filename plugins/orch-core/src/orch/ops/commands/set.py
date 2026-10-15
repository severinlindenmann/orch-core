"""orch set: change ticket fields"""

from typing import Any

from orch.ops import plans
from orch.ops._dsl import REF, SET_PATTERN, STR, L, arr, err, obj, operation
from orch.ops.base import Context, Result


def handle(ctx: Context, args: dict[str, Any]) -> Result:
    return plans.run(ctx, "set", args, plans.set_fields)


OP = operation(
    "set",
    "Edit",
    "Set ticket fields: title, priority, size, labels, due, links, parent, blocked_by.",
    who="agent",
    props={
        "ref": REF("ticket REF"),
        "pairs": L(
            "key=value pairs; keys: title priority size labels due links parent blocked_by",
            items={"type": "string", "pattern": SET_PATTERN},
            **{"x-metavar": "KEY=VALUE"},
        ),
    },
    required=("ref", "pairs"),
    positional=("ref", "pairs"),
    pre=("ticket_exists", "ticket_open_for_work", "base_rev_tracked", "grant_valid", "text_clean"),
    emits=("ticket.updated",),
    text="ok {key} ticket.updated {fields} seq={seq}\nnext: {next}",
    data=obj({"fields": arr(STR)}),
    errors=(err("conflict.field"), err("transition.refused"), err("not_found"), err("parse.text")),
    handler=handle,
)
