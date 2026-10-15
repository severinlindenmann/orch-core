"""orch inbox: tickets handed over from peer workspaces"""

from typing import Any

from orch.ops import decisions, views
from orch.ops._dsl import INT, KEY, STR, arr, obj, operation
from orch.ops.base import Context, Result
from orch.ops.runtime import Call, flat
from orch.ops.views import fence

PEER_LABEL = "from-peer"


def handle(ctx: Context, args: dict[str, Any]) -> Result:
    """Tickets a pinned peer workspace handed over arrive labelled ``from-peer`` (F1 10.6); a label ``peer.<name>``
    names the sender (F1 is silent on how: until linked workspaces exist, that is the convention). Also the decisions
    on the session's claimed tickets that ``wait`` has not handed over: an explicit ``inbox`` acknowledges them."""
    c = Call.of(ctx, "inbox")
    uids = [u for (u,) in c.store.index.query("SELECT uid FROM ticket_labels WHERE label = ?", (PEER_LABEL,))]
    found = []
    for _uid, v in views.verified(c, uids):
        if PEER_LABEL in v.fields["labels"] and v.status not in ("done", "closed"):
            found.append(v)
    items = []
    for v in sorted(found, key=lambda v: views.key_number(v.key)):
        labels = v.fields["labels"]
        sender = next((x[5:] for x in labels if x.startswith("peer.") and len(x) > 5), "unknown")
        items.append({"key": v.key, "from": sender, "title": flat(v.title)})
    pending = []
    for v in c.mine(live_only=False):
        evs = decisions.undelivered(c, v)
        for e in evs:
            pending.append({"key": v.key, "seq": e["seq"], "decision": decisions.line(e)})
        if evs:
            c.notes.update(v.uid, now=c.now, decided=evs[-1]["seq"], keep=False)
    lines = []
    if items:
        lines += fence(
            "\n".join(f"{i['key']} from={views.short(i['from'], 32)}: {views.short(i['title'], 80)}" for i in items),
            "inbox",
        )
    if pending:
        lines += fence("\n".join(f"{d['key']} {d['decision']}" for d in pending), "decisions")
    if not lines:
        lines = ["inbox is empty"]
    hints = [f"orch show {items[0]['key']}"] if items else ["orch next"]
    if pending:
        hints = [f"orch show {pending[0]['key']}"]
    data = {"count": len(items) + len(pending), "items": items, "decisions": pending}
    return Result(data=data, hints=hints, lines=lines)


OP = operation(
    "inbox",
    "Read",
    "Tickets handed over from peer workspaces, and decisions on your tickets that wait has not handed over.",
    who="read",
    pre=("workspace_exists",),
    text="ok inbox {count}\nnext: {next}",
    data=obj(
        {
            "count": INT,
            "items": arr(obj({"key": KEY, "from": STR, "title": STR})),
            "decisions": arr(obj({"key": KEY, "seq": INT, "decision": STR})),
        }
    ),
    handler=handle,
)
