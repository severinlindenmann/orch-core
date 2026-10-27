"""Read one follow-up from the verified ticket view."""

from orch.model import followup_events as fe
from orch.ops import followup, views
from orch.ops._dsl import S, err, operation
from orch.ops.base import Result
from orch.ops.runtime import Call


def handle(ctx, args):
    c = Call.of(ctx, "follow_up.show")
    view, rec = followup.find(c, args["id"])
    data = dict(rec)
    lines = [f"{name}: {rec.get(name) or '-'}" for name in ("state", "trigger", "due", "deadline")]
    lines += [f"set by: {rec['person']} ({rec['setter_session']})"]
    on = rec.get("on")
    if on:
        lines += [
            "waiting for: "
            + (
                f"webhook (hook {rec['hook_prefix']})"
                if on["source"] == "webhook"
                else f"{on['kind']}.{on['status']}" + (f":{on['ref']}" if on.get("ref") else "")
            )
        ]
    # the closed wake reason comes before the brief, rendered by core from closed values only
    if wake := fe.wake_line(
        rec["trigger"] if not rec.get("fallback") else "deadline", rec.get("fallback"), rec.get("event")
    ):
        lines += [wake]
        data["wake"] = wake
    lines += views.fence(rec["brief"], "follow-up brief")
    lines += [
        f"outcome: {rec['outcome'] or '-'}",
        f"reason: {rec['reason'] or '-'}",
        f"late by: {rec['late_by'] or 0}s",
    ]
    return Result(data=data, key=view.key, lines=lines)


OP = operation(
    "follow_up.show",
    "Read",
    "Show a follow-up and its brief.",
    who="read",
    props={"id": S("follow-up ID")},
    required=("id",),
    positional=("id",),
    text="ok {key} followup {id}",
    data={"type": "object"},
    errors=(err("not_found"),),
    handler=handle,
    addon="schedules",
)
