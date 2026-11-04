"""Read follow-ups across visible tickets, in due order."""

from orch.model.types import ts
from orch.ops import followup
from orch.ops._dsl import INT, REF, B, arr, err, obj, operation
from orch.ops.base import Result
from orch.ops.runtime import Call


def handle(ctx, args):
    c = Call.of(ctx, "follow_up.list")
    tickets = [c.resolve(args["ref"], live_only=False)] if args.get("ref") else followup.tickets(c)
    recent_since = c.now - 30 * 86400
    rows = [
        {**dict(f), "key": v.key}
        for v in tickets
        for f in v.followups
        if f["state"] in ("pending", "needs_person")
        or (args.get("all") and ts(f["fired_at"] or f["set_at"]) >= recent_since)
    ]
    rows.sort(key=lambda f: (f["due"] or f["deadline"] or "", f["id"]))
    return Result(
        data={"rows": rows, "total": len(rows)},
        lines=[
            f"{f['key']} {f['id']} due={f['due'] or f['deadline']} trigger={f['trigger']} state={f['state']}"
            for f in rows
        ],
    )


OP = operation(
    "follow_up.list",
    "Read",
    "List pending follow-ups across visible tickets.",
    who="read",
    props={"ref": REF(), "all": B("include completed and cancelled follow-ups from the last 30 days")},
    positional=("ref",),
    text="ok followup.list {total}",
    data=obj({"rows": arr({"type": "object"}), "total": INT}),
    errors=(err("not_found"),),
    handler=handle,
    addon="schedules",
)
