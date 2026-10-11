"""orch status: who you are, your grant, your cursor and what needs you"""

from typing import Any

from orch.ops import views
from orch.ops._dsl import INT, KEY, STR, B, err, obj, operation
from orch.ops.base import Context, Result
from orch.ops.runtime import Call, flat


def handle(ctx: Context, args: dict[str, Any]) -> Result:
    c = Call.of(ctx, "status")
    ws = c.store.state.workspace
    person = c.person
    grant = None
    name = "unattended"
    if person is not None:
        g = ws.grants[c.grant_id]
        grant = f"{c.grant_id} until {g.expires_at}"
        m = ws.members.get(person)
        name = m.name if m else person
    mine = c.mine() if ctx.session else []
    data: dict[str, Any] = {"person": flat(name), "cursor": 0}
    if grant:
        data["grant"] = grant
    lines: list[str] = []
    hints: list[str] = []
    view = None
    if len(mine) == 1:
        view = mine[0]
        head = c.store.head_seq(view.uid)
        cursor = c.cursor(view.uid)
        data.update(claim=view.key, cursor=cursor, new_events=max(0, head - cursor))
        t = views.next_task(view, ctx.session)
        bits = [f"{view.key} {view.status} (your claim)"]
        if t is not None:
            bits.append(f"{t.id} next")
        bits.append(f"{max(0, head - cursor)} new events")
        needs = [f"{q.id} blocking" for q in views.open_questions(view) if q.blocking]
        if needs:
            bits.append("waiting: " + ", ".join(needs))
        lines.append(" \u00b7 ".join(bits))
        hints = [views.next_hint(view, ctx.session)]
    elif mine:
        lines.append("claims: " + ", ".join(v.key for v in mine))
        hints = ["orch show REF"]
    else:
        lines.append("no claim")
    if not hints:
        hints = [views.claim_hint(c)]
    if args.get("verbose"):
        data["state_dir"] = str(c.ws.state_dir)
        lines.append(f"state dir {c.ws.state_dir}")
    if c.ws.pin_created:
        data["pin"] = "created"
        lines.append("genesis pin created by this call")
    return Result(data=data, key=view.key if view else None, cursor=data["cursor"], hints=hints, lines=lines)


OP = operation(
    "status",
    "Context",
    "Who you are, your grant, your cursor, your claim and what needs you.",
    who="read",
    props={"verbose": B("also show the state directory")},
    pre=("workspace_exists",),
    text="ok status person={person} cursor={cursor}[ grant={grant}][ claim={claim}]\nnext: {next}",
    data=obj(
        {"person": STR, "grant": STR, "claim": KEY, "cursor": INT, "new_events": INT, "state_dir": STR, "pin": STR},
        optional=("grant", "claim", "new_events", "state_dir", "pin"),
    ),
    errors=(err("not_found", "run orch init in a workspace", ["orch", "describe", "init"]),),
    handler=handle,
)
