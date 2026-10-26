"""orch widgets types: the widget types an agent may write, each with its shape and a working example"""

from typing import Any

from orch.ops._dsl import INT, STR, arr, obj, operation
from orch.ops.base import Context, Result


def handle(ctx: Context, args: dict[str, Any]) -> Result:
    import json

    from orch.widgets import catalog

    rows = [
        {"type": name, "shows": shows, "shape": shape, "example": json.dumps(example, ensure_ascii=False)}
        for name, (shows, shape, example) in catalog.TYPES.items()
    ]
    return Result(
        data={"count": len(rows), "types": rows}, lines=catalog.types_lines(), hints=["orch widgets check FILE"]
    )


OP = operation(
    "widgets.types",
    "Context",
    "List the widget types you may write in a ticket (charts, tables, checks) with a working example of each.",
    who="read",
    text="ok widgets.types {count}\nnext: {next}",
    data=obj({"count": INT, "types": arr(obj({"type": STR, "shows": STR, "shape": STR, "example": STR}))}),
    handler=handle,
)
