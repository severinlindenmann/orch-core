"""Mint a webhook doorbell address for a follow-up (#717). Nothing is stored here: the address starts working when a
follow-up is set with ``--on webhook:<id>`` (the ledger then keeps only the hash of the id)."""

from orch.model import followup_events as fe
from orch.ops import followup
from orch.ops._dsl import REF, STR, S, err, obj, operation
from orch.ops.base import Result
from orch.ops.errors import OrchError
from orch.ops.runtime import Call

HINT = (
    "this host does not receive webhooks: an owner starts it with: orch host run --hook-listen HOST:PORT --hook-url URL"
)


def handle(ctx, args):
    c = Call.of(ctx, "webhook.new")
    followup.require_on(c.store.state.workspace.addons)
    view = c.resolve(args["ref"], live_only=True)
    base = (c.ctx.caps.env.get("ORCH_HOOK_BASE") or "").rstrip("/")
    if not base:
        raise OrchError("followup.no_receiver", HINT)
    label = c.text(args["label"], one_line=True, limit=200, what="label")
    hook = fe.new_hook_id()
    url = f"{base}/h/{hook}"
    lines = [
        f"url: {url}",
        "shown once: put it where the outside job ends, e.g. curl -X POST <url> as the last step",
        f"then: orch follow-up {view.key} --on webhook:{hook} --in 3d --brief-file brief.md",
        "the body of a request is never read; a ring only wakes the follow-up",
    ]
    return Result(data={"id": hook, "url": url, "label": label}, key=view.key, lines=lines)


OP = operation(
    "webhook.new",
    "Lifecycle",
    "Make a doorbell address that wakes one follow-up when an outside job calls it.",
    who="read",
    props={"ref": REF(), "label": S("what it is for", maxLength=200)},
    required=("label",),
    positional=("ref",),
    text="ok {key} webhook {label}",
    data=obj({"id": STR, "url": STR, "label": STR}),
    errors=(err("followup.off"), err("followup.no_receiver"), err("not_found"), err("parse.text")),
    handler=handle,
)
