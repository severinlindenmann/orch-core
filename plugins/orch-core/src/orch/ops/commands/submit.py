"""orch submit: move the ticket to testing"""

from typing import Any

from orch.ops import plans
from orch.ops._dsl import REF, STR, err, obj, operation
from orch.ops.base import Context, Result
from orch.ops.errors import OrchError
from orch.ops.runtime import Call, Projection, flat
from orch.store import StoreError


def handle(ctx: Context, args: dict[str, Any]) -> Result:
    return plans.run(ctx, "submit", args, build, claim=True)


def build(c: Call, p: Projection, args: dict[str, Any]) -> plans.Out:
    try:
        p.add({"type": "ticket.submitted"})
    except StoreError as e:
        if e.code != "submit.incomplete":
            raise
        # the model lists everything that is missing; evidence is the case this operation names
        code = "ac.evidence_missing" if "no evidence" in e.detail else "transition.refused"
        gate = next((g for g in ("requirements", "plan") if f"{g} not approved" in e.detail), None)
        section = next((s for s in ("verification", "findings") if f"section {s} is empty" in e.detail), None)
        hint = None
        if gate:
            hint = f'orch ask "approve the {gate} gate?"'
        elif section:
            hint = f"orch section set {section} -m TEXT"
        raise OrchError(code, f"submit: {flat(e.detail)[:180]}", hint=hint) from None
    return plans.Out({"status": p.last_view.status}, ["orch wait"])


OP = operation(
    "submit",
    "Lifecycle",
    "Move the ticket to testing; every acceptance criterion needs evidence first.",
    who="agent",
    props={"ref": REF()},
    positional=("ref",),
    pre=("ticket_exists", "session_holds_claim", "all_ac_have_evidence", "grant_valid"),
    emits=("ticket.submitted",),
    text="ok {key} ticket.submitted testing seq={seq}\nnext: {next}",
    data=obj({"status": STR}),
    errors=(
        err("ac.evidence_missing"),
        err("claim.required"),
        err("transition.refused"),
        err("not_found"),
        err("ambiguous_ref"),
    ),
    handler=handle,
)
