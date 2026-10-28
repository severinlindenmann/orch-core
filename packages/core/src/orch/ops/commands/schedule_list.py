"""List manual schedules from verified visible tickets."""

from orch.ops import followup, schedule
from orch.ops._dsl import INT, REF, arr, err, obj, operation
from orch.ops.base import Result
from orch.ops.runtime import Call


def handle(ctx, args):
    c = Call.of(ctx, "schedule.list")
    tickets = [c.resolve(args["ref"], live_only=False)] if args.get("ref") else followup.tickets(c)
    rows = [{**schedule.thaw(r), "key": v.key} for v in tickets for r in v.schedules]
    return Result(
        data={"rows": rows, "total": len(rows)},
        lines=[
            f"{r['id']} {r['state']}: "
            + schedule.describe(r["rule"], r["key"], r["ends"], r.get("target", "existing"), r.get("title"))
            for r in rows
        ],
    )


OP = operation(
    "schedule.list",
    "Read",
    "List manual schedules across visible tickets.",
    who="read",
    props={"ref": REF()},
    positional=("ref",),
    text="ok schedule.list {total}",
    data=obj({"rows": arr({"type": "object"}), "total": INT}),
    errors=(err("not_found"),),
    handler=handle,
    addon="schedules",
)
