"""Set a time follow-up and release the claim in the same event."""

from orch.identity import new_ulid
from orch.ops import followup, plans
from orch.ops._dsl import FILE, REF, STR, S, err, obj, operation
from orch.ops.errors import OrchError


def handle(ctx, args):
    result = plans.run(ctx, "follow_up", args, build)
    result.head = (
        f"ok {result.key} followup.set {result.data['id']} "
        f"due={followup.local_time(result.data['due'])} state={result.data['state']}"
    )
    return result


def build(c, p, args):
    followup.require_on(c.store.state.workspace.addons)
    stamp = followup.due(args, c.now)
    if ("brief" in args) == ("brief_file" in args):
        raise OrchError("invalid.input", "give exactly one of --brief or --brief-file")
    brief = c.read_text_source({"text": args["brief"]} if "brief" in args else {"file": args["brief_file"]})
    fid = "fu_" + new_ulid()
    event = {
        "type": "followup.set",
        "followup": fid,
        "trigger": "time",
        "due": stamp,
        "deadline": stamp,
        "brief": brief,
    }
    if "label" in args:
        event["label"] = c.text(args["label"], one_line=True, limit=200, what="label")
    view = followup.add(p, event)
    rec = next(f for f in view.followups if f["id"] == fid)
    data = {k: rec[k] for k in ("id", "due", "deadline", "state", "label") if k in rec}
    return plans.Out(data, [f"orch follow-up show {fid}; end your session now"])


OP = operation(
    "follow_up",
    "Lifecycle",
    "Schedule a fresh session, release your claim, then end this session.",
    who="agent",
    props={
        "ref": REF(),
        "in": S("duration: 90m, 36h or 2d", **{"x-metavar": "DURATION"}),
        "at": S("local time: YYYY-MM-DDTHH:MM[:SS] or +2d 09:00", **{"x-metavar": "WHEN"}),
        "brief_file": FILE,
        "brief": S("instructions for the fresh session", **{"x-metavar": "TEXT"}),
        "label": S("optional label", maxLength=200),
    },
    positional=("ref",),
    one_of=("in", "at"),
    emits=("followup.set",),
    text="ok {key} followup.set {id} due={due} state={state}\nnext: {next}",
    data=obj({"id": STR, "due": STR, "deadline": STR, "state": STR, "label": STR}, optional=("label",)),
    errors=tuple(
        err(c)
        for c in (
            "followup.off",
            "followup.denied",
            "followup.too_soon",
            "followup.too_far",
            "followup.bad_brief",
            "not_found",
            "parse.text",
        )
    ),
    handler=handle,
)
OP.declaration["input"]["allOf"] = [{"oneOf": [{"required": ["brief"]}, {"required": ["brief_file"]}]}]
