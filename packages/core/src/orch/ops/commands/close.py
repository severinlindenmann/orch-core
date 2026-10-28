"""orch close: close a ticket"""

from typing import Any

from orch.ops._dsl import MSG, REF, STR, E, S, err, obj, operation
from orch.ops.base import Context, Result
from orch.ops.errors import OrchError
from orch.ops.human import Human


def handle(ctx: Context, args: dict[str, Any]) -> Result:
    if ctx.grant is not None:
        from orch.model.schedules_wire import child_run
        from orch.ops import plans
        from orch.ops.runtime import Call

        c = Call.of(ctx, "close")
        c.store.load_all()
        core = c.store.state._core
        if not child_run(core, core.ws.grants.get(ctx.grant.partition(".")[0])):
            raise OrchError("human_only", "only a schedule run may close its child")

        def build(c, p, args):
            event = {"type": "ticket.closed", "resolution": args.get("resolution", "other")}
            if args.get("duplicate_of"):
                event["duplicate_of"] = c.store.normalise_ref(args["duplicate_of"])
            text = c.read_text_source(args, ("message",))
            if text is not None:
                event["text"] = c.text(text, what="the reason")
            p.add(event)
            return plans.Out(data={"resolution": event["resolution"]}, hints=["orch list"])

        return plans.run(ctx, "close", args, build, live_only=False)
    h = Human(ctx, "close")
    view = h.ticket(args.get("ref"))
    resolution = args.get("resolution", "other")
    if args.get("duplicate_of") and resolution != "duplicate":
        raise OrchError("invalid.input", "--duplicate-of goes with --resolution duplicate")
    event: dict[str, Any] = {"type": "ticket.closed", "resolution": resolution}
    if args.get("duplicate_of"):
        dup = h.store.normalise_ref(args["duplicate_of"])
        h.ticket(dup)  # loaded for the model; one not_found for "no such ticket" and "you may not see it" alike
        event["duplicate_of"] = dup
    text = h.text(args, what="the reason")
    if text is not None:
        event["text"] = text
    done = h.run(event, view.uid, f"close {view.key} as {resolution}", review=h.header(view, "title: " + view.title))
    return h.ticket_result(view, done, {"resolution": resolution}, "orch list")


OP = operation(
    "close",
    "Human only",
    "Close a ticket without finishing it.",
    who="human",
    props={
        "ref": REF("ticket REF"),
        "resolution": E("why", "wont_do", "duplicate", "obsolete", "other", "nothing_to_do", default="other"),
        "duplicate_of": S("the surviving ticket (with --resolution duplicate)", **{"x-metavar": "KEY"}),
        "message": MSG,
    },
    positional=("ref",),
    pre=("ticket_exists", "ticket_owner_or_maintainer", "user_presence", "text_clean"),
    emits=("ticket.closed",),
    text="ok {key} ticket.closed {resolution} seq={seq}\nnext: {next}",
    data=obj({"resolution": STR}),
    errors=(err("role.denied"), err("transition.refused"), err("parse.text"), err("not_found")),
    handler=handle,
)
