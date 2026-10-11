"""orch handoff: write where it stands and release the claim"""

from typing import Any

from orch.ops import plans, views
from orch.ops._dsl import FILE, INT, MSG, REF, err, obj, operation
from orch.ops.base import Context, Result
from orch.ops.errors import OrchError
from orch.ops.runtime import Call, Projection


def handle(ctx: Context, args: dict[str, Any]) -> Result:
    return plans.run(ctx, "handoff", args, build, claim=True)


def build(c: Call, p: Projection, args: dict[str, Any]) -> plans.Out:
    c.require_claim(p.view, own=True)
    text = c.text(c.read_text_source(args, ("message", "file")) or "", limit=2048, what="handoff").strip("\n")
    if not text:
        raise OrchError("invalid.input", "the handoff is empty")
    p.add({"type": "handoff.written", "text": text})
    p.add({"type": "claim.released", "session": p.view.claim.session, "reason": "handoff"})
    return plans.Out({"bytes": len(text.encode("utf-8"))}, [views.claim_hint(c)])


OP = operation(
    "handoff",
    "Lifecycle",
    "Write the current state (at most 2048 bytes) and release the claim.",
    who="agent",
    props={"ref": REF(), "message": MSG, "file": FILE},
    positional=("ref",),
    one_of=("message", "file"),
    pre=("ticket_exists", "session_holds_claim", "grant_valid", "text_clean"),
    emits=("handoff.written", "claim.released"),
    text="ok {key} handoff.written {bytes} seq={seq}\nnext: {next}",
    data=obj({"bytes": INT}),
    errors=(err("claim.required"), err("parse.text"), err("not_found"), err("ambiguous_ref")),
    handler=handle,
)
