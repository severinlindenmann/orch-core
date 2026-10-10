"""orch list: list tickets"""

from typing import Any

from orch.canon.text import clean_line
from orch.cli.render import fence
from orch.ops import views
from orch.ops._dsl import INT, KEY, STR, B, E, I, S, arr, obj, operation
from orch.ops.base import Context, Result
from orch.ops.runtime import Call


def handle(ctx: Context, args: dict[str, Any]) -> Result:
    c = Call.of(ctx, "list")
    c.store.load_all()
    mine_keys = {v.key for v in c.mine()} if args.get("mine") else set()
    rows = []
    for v in c.store.state.tickets.values():
        if not c.sees(v):
            continue
        if args.get("status") and v.status != args["status"]:
            continue
        if args.get("label") and args["label"] not in v.fields["labels"]:
            continue
        if args.get("mine"):
            me = c.person
            assigned = me is not None and (v.owner == me or me in v.people["assignees"])
            if v.key not in mine_keys and not assigned:
                continue
        rows.append(v)
    rows.sort(key=lambda v: views.key_number(v.key), reverse=True)
    rows = rows[: args.get("limit", 20)]
    tickets = [{"key": v.key, "title": clean_line(v.title), "status": v.status} for v in rows]
    body = "\n".join(f"{v.key} {v.status} {v.fields['priority']}: {views.short(v.title, 80)}" for v in rows)
    return Result(
        data={"count": len(rows), "tickets": tickets},
        hints=[f"orch show {rows[0].key}" if rows else "orch new TITLE"],
        lines=fence(body, "tickets") if rows else ["no tickets"],
    )


OP = operation(
    "list",
    "Read",
    "List tickets, newest first.",
    who="read",
    props={
        "status": E("only this status", "backlog", "open", "in_progress", "testing", "done", "closed"),
        "mine": B("only tickets you hold or are assigned"),
        "label": S("only this label"),
        "limit": I("at most this many", minimum=1, maximum=200, default=20, **{"x-metavar": "N"}),
    },
    pre=("workspace_exists",),
    text="ok list {count}\nnext: {next}",
    data=obj({"count": INT, "tickets": arr(obj({"key": KEY, "title": STR, "status": STR}))}),
    handler=handle,
)
