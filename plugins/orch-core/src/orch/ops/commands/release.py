"""orch release: let go of the claim"""

from typing import Any

from orch.ops import plans
from orch.ops._dsl import BOOL, REF, err, obj, operation
from orch.ops.base import Context, Result
from orch.ops.runtime import Call, Projection


def handle(ctx: Context, args: dict[str, Any]) -> Result:
    return plans.run(ctx, "release", args, build, live_only=False)


def build(c: Call, p: Projection, args: dict[str, Any]) -> plans.Out:
    c.require_claim(p.view, live_only=False)
    p.add({"type": "claim.released", "session": p.view.claim.session, "reason": "released"})
    return plans.Out({"released": True}, ["orch claim --next"])


OP = operation(
    "release",
    "Lifecycle",
    "Release your claim without a handoff.",
    who="agent",
    props={"ref": REF()},
    positional=("ref",),
    pre=("ticket_exists", "session_holds_claim", "grant_valid"),
    emits=("claim.released",),
    text="ok {key} claim.released released seq={seq}\nnext: {next}",
    data=obj({"released": BOOL}),
    errors=(err("claim.required"), err("claim.not_live"), err("not_found"), err("ambiguous_ref")),
    handler=handle,
)
