"""orch status: who you are, your grant, your cursor and what needs you"""

from typing import Any

from orch.ops import views
from orch.ops._dsl import INT, KEY, STR, B, err, obj, operation
from orch.ops.base import Context, Result
from orch.ops.runtime import Call, flat


def _local(iso: str) -> str:
    """The end of a grant in this computer's time with its zone (``Wed 11:10 CEST``); the text of ``orch status`` shows
    this, ``--json`` keeps the signed UTC time."""
    import time

    from orch.host.signcontent import local_time

    try:
        from datetime import UTC, datetime

        hours = (datetime.strptime(iso, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC).timestamp() - time.time()) / 3600
    except ValueError:
        hours = None
    return local_time(iso, hours) or iso


def handle(ctx: Context, args: dict[str, Any]) -> Result:
    c = Call.of(ctx, "status")
    ws = c.store.state.workspace
    person = c.viewer  # direct mode: the grant's person; host mode: the viewer (column P: the mapped person)
    grant = grant_local = None
    name = "unattended"
    if person is not None:
        g = ws.grants.get(c.grant_id) if c.grant_id and c.person is not None else None
        if g is not None:
            grant = f"{c.grant_id} until {g.expires_at}"
            grant_local = f"{c.grant_id} until {_local(g.expires_at)}"
        m = ws.members.get(person)
        name = m.name if m else person
    mine = c.mine() if ctx.session else []
    data: dict[str, Any] = {"person": flat(name), "cursor": 0}
    if grant:
        data["grant"] = grant
        data["grant_local"] = grant_local
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
        hints = [
            views.human_next_hint(c) if ctx.hosted and person is not None and not c.grant_id else views.claim_hint(c)
        ]
    if args.get("verbose") and not ctx.hosted:  # host mode: no host path is ever returned
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
    text="ok status person={person} cursor={cursor}[ grant={grant_local}][ claim={claim}]\nnext: {next}",
    data=obj(
        {
            "person": STR,
            "grant": STR,
            "grant_local": STR,
            "claim": KEY,
            "cursor": INT,
            "new_events": INT,
            "state_dir": STR,
            "pin": STR,
        },
        optional=("grant", "grant_local", "claim", "new_events", "state_dir", "pin"),
    ),
    errors=(err("not_found", "run it in a workspace folder (orch setup makes one)", ["orch", "help"]),),
    handler=handle,
)
