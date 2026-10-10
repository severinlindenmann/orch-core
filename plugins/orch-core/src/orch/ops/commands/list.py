"""orch list: list tickets"""

from typing import Any

from orch.ops import views
from orch.ops._dsl import INT, KEY, STR, B, E, I, S, arr, obj, operation
from orch.ops.base import Context, Result
from orch.ops.runtime import Call, flat
from orch.ops.views import fence

MORE_SCAN = 50  # candidates looked at beyond the limit to count what is left


def handle(ctx: Context, args: dict[str, Any]) -> Result:
    """Candidates come from the index (a hint); every ticket printed is loaded and verified first, and checked again
    against the filters, so nothing unverified is shown."""
    c = Call.of(ctx, "list")
    where, params = [], []
    if args.get("status"):
        where.append("status = ?")
        params.append(args["status"])
    if args.get("label"):
        where.append("uid IN (SELECT uid FROM ticket_labels WHERE label = ?)")
        params.append(args["label"])
    me = c.person
    mine_keys: set[str] = set()
    if args.get("mine"):
        mine_keys = {v.key for v in c.mine()}
        ids = c.claimed_uids()
        marks = ",".join("?" for _ in ids)
        parts = [f"uid IN ({marks})"] if ids else []
        if me is not None:
            parts += ["owner = ?", "uid IN (SELECT uid FROM ticket_people WHERE role = 'assignees' AND person = ?)"]
        where.append("(" + " OR ".join(parts) + ")" if parts else "0")
        params += [*ids, *([me, me] if me is not None else [])]
    sql = "SELECT uid, key FROM tickets" + (" WHERE " + " AND ".join(where) if where else "")
    cands = sorted(c.store.index.query(sql, params), key=lambda r: views.key_number(r[1]), reverse=True)
    limit = args.get("limit", 20)
    rows: list[Any] = []
    extra = 0  # visible, matching tickets beyond the limit, counted only from verified tickets
    scanned_extra = 0
    for _uid, v in views.verified(c, (r[0] for r in cands)):
        if len(rows) >= limit and scanned_extra >= MORE_SCAN:
            break
        if len(rows) >= limit:
            scanned_extra += 1
        if args.get("status") and v.status != args["status"]:
            continue
        if args.get("label") and args["label"] not in v.fields["labels"]:
            continue
        if args.get("mine") and not (
            v.key in mine_keys or (me is not None and (v.owner == me or me in v.people["assignees"]))
        ):
            continue
        if len(rows) < limit:
            rows.append(v)
        else:
            extra += 1
    exhausted = scanned_extra < MORE_SCAN
    tickets = [{"key": v.key, "title": flat(v.title), "status": v.status} for v in rows]
    body = "\n".join(f"{v.key} {v.status} {v.fields['priority']}: {views.short(v.title, 80)}" for v in rows)
    lines = fence(body, "tickets") if rows else ["no tickets"]
    if extra:  # only tickets the actor can see are counted: "N+" when the look was cut short
        count = f"+{extra} more" if exhausted else f"{extra} or more not shown"
        lines.append(f"{count} (orch list --limit N, or narrow it with --status or --label)")
    return Result(
        data={"count": len(rows), "tickets": tickets},
        hints=[f"orch show {rows[0].key}" if rows else "orch new TITLE"],
        lines=lines,
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
