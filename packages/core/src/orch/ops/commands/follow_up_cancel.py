"""Cancel a pending follow-up by ID or ticket reference."""

from orch.ops import followup
from orch.ops._dsl import STR, S, err, obj, operation
from orch.ops.errors import OrchError
from orch.ops.runtime import Call


def handle(ctx, args):
    c = Call.of(ctx, "follow_up.cancel")
    with c.locked():
        target = args["id_or_ref"]
        if target.startswith("fu_"):
            view, rec = followup.find(c, target)
        else:
            view = c.resolve(target, live_only=False)
            rec = next((f for f in view.followups if f["state"] in ("pending", "needs_person")), None)
            if rec is None:
                raise OrchError("not_found", f"{view.key} has no pending follow-up")
        actor = ctx.prepare.actor if ctx.prepare is not None else c.actor()
        reason = "person" if actor["kind"] == "person" else "agent"
        if args.get("reason") == "dismissed":
            reason = "dismissed"
        p = c.projection(view)
        followup.add(p, {"type": "followup.cancelled", "followup": rec["id"], "reason": reason})
        done = p.commit()
        return c.result(
            p.last_view,
            {"id": rec["id"], "state": "cancelled", "reason": reason},
            seq=done[-1].event["seq"] if done else p.last_seq,
            hints=[f"orch follow-up show {rec['id']}"],
        )


OP = operation(
    "follow_up.cancel",
    "Lifecycle",
    "Cancel a pending follow-up.",
    who="agent",
    props={
        "id_or_ref": S("follow-up ID or ticket REF"),
        "reason": S("cancellation reason; dismissed marks a dismissal"),
    },
    required=("id_or_ref",),
    positional=("id_or_ref",),
    emits=("followup.cancelled",),
    text="ok {key} followup.cancelled {id} state=cancelled\nnext: {next}",
    data=obj({"id": STR, "state": STR, "reason": STR}),
    errors=tuple(err(c) for c in ("followup.denied", "not_found", "ambiguous_ref")),
    handler=handle,
    addon="schedules",
)
