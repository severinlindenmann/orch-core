"""orch grant revoke: revoke a grant"""

from typing import Any

from orch.ops._dsl import STR, S, err, obj, operation
from orch.ops.base import Context, Result
from orch.ops.errors import OrchError
from orch.ops.human import Human


def handle(ctx: Context, args: dict[str, Any]) -> Result:
    h = Human(ctx, "grant.revoke")
    gid = args["grant"]
    h.who()
    if gid not in h.store.state.workspace.grants:
        raise OrchError("not_found", f"no grant {gid}")
    event: dict[str, Any] = {"type": "grant.revoked", "grant": gid}
    if args.get("reason"):
        event["reason"] = h.call.text(args["reason"], one_line=True, what="reason")
    done = h.run(event, "workspace", f"revoke grant {gid}")
    return h.workspace_result(done, {"grant": gid}, "orch grant")


OP = operation(
    "grant.revoke",
    "Human only",
    "Revoke a grant; every session using it ends.",
    who="human",
    props={
        "grant": S("grant id", pattern=r"^gr_[0-7][0-9A-HJKMNP-TV-Z]{25}$", **{"x-metavar": "GRANT"}),
        "reason": S("why", **{"x-metavar": "TEXT"}),
    },
    required=("grant",),
    positional=("grant",),
    pre=("workspace_exists", "role_allows", "user_presence", "text_clean"),
    emits=("grant.revoked",),
    text="ok grant.revoked {grant} seq={seq}\nnext: {next}",
    data=obj({"grant": STR}),
    errors=(err("role.denied"), err("parse.text"), err("not_found")),
    handler=handle,
)
