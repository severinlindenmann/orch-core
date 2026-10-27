"""Read the signed brief from the verified ledger, never from a launch prompt."""

from orch.ops import schedule, views
from orch.ops._dsl import S, err, operation
from orch.ops.base import Result
from orch.ops.runtime import Call


def handle(ctx, args):
    view, rec = schedule.find(Call.of(ctx, "schedule.show"), args["id"])
    return Result(
        data=schedule.thaw(rec),
        key=view.key,
        lines=[schedule.describe(rec["rule"], view.key, rec["ends"]), *views.fence(rec["brief"], "schedule brief")],
    )


OP = operation(
    "schedule.show",
    "Read",
    "Show a schedule and its signed brief.",
    who="read",
    props={"id": S("schedule ID")},
    required=("id",),
    positional=("id",),
    text="ok {key} schedule {id}",
    data={"type": "object"},
    errors=(err("not_found"),),
    handler=handle,
    addon="schedules",
)
