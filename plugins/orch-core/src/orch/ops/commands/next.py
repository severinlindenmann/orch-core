"""orch next: the ticket to work on next"""

from typing import Any

from orch.ops import views
from orch.ops._dsl import STR, obj, operation
from orch.ops.base import Context, Result
from orch.ops.runtime import Call, flat
from orch.ops.views import fence


def handle(ctx: Context, args: dict[str, Any]) -> Result:
    c = Call.of(ctx, "next")
    view = views.next_ticket(c)
    if view is None:
        return Result(data={"target": "none"}, hints=["orch new TITLE"], lines=["nothing is free to work on"])
    mine = view.claim is not None and view.claim.live and view.uid in {v.uid for v in c.mine()}
    data = {"target": view.key, "title": flat(view.title)}
    return Result(
        data=data,
        key=view.key,
        seq=c.store.head_seq(view.uid),
        cursor=c.cursor(view.uid),
        hints=[f"orch show {view.key}" if mine else f"orch claim {view.key}"],
        lines=fence(
            f"{view.key} {view.status} {view.fields['priority']}: {views.short(view.title, 100)}", "next ticket"
        ),
    )


OP = operation(
    "next",
    "Read",
    "The ticket to work on next.",
    who="read",
    pre=("workspace_exists",),
    text="ok next {target}\nnext: {next}",
    data=obj({"target": STR, "title": STR}, optional=("title",)),
    handler=handle,
)
