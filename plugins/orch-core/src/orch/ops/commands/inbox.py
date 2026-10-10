"""orch inbox: tickets handed over from peer workspaces"""

from typing import Any

from orch.ops import views
from orch.ops._dsl import INT, KEY, STR, arr, obj, operation
from orch.ops.base import Context, Result
from orch.ops.runtime import Call, flat
from orch.ops.views import fence

PEER_LABEL = "from-peer"


def handle(ctx: Context, args: dict[str, Any]) -> Result:
    """Tickets a pinned peer workspace handed over arrive labelled ``from-peer`` (F1 10.6); a label ``peer.<name>``
    names the sender (F1 is silent on how: until linked workspaces exist, that is the convention)."""
    c = Call.of(ctx, "inbox")
    c.store.load_all()
    items = []
    for v in sorted(c.store.state.tickets.values(), key=lambda v: views.key_number(v.key)):
        labels = v.fields["labels"]
        if PEER_LABEL in labels and c.sees(v) and v.status not in ("done", "closed"):
            sender = next((x[5:] for x in labels if x.startswith("peer.") and len(x) > 5), "unknown")
            items.append({"key": v.key, "from": sender, "title": flat(v.title)})
    lines = (
        fence(
            "\n".join(f"{i['key']} from={views.short(i['from'], 32)}: {views.short(i['title'], 80)}" for i in items),
            "inbox",
        )
        if items
        else ["inbox is empty"]
    )
    hints = [f"orch show {items[0]['key']}"] if items else ["orch next"]
    return Result(data={"count": len(items), "items": items}, hints=hints, lines=lines)


OP = operation(
    "inbox",
    "Read",
    "Tickets handed over from peer workspaces.",
    who="read",
    pre=("workspace_exists",),
    text="ok inbox {count}\nnext: {next}",
    data=obj({"count": INT, "items": arr(obj({"key": KEY, "from": STR, "title": STR}))}),
    handler=handle,
)
