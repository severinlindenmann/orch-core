"""orch release: let go of the claim"""

from orch.ops._dsl import BOOL, REF, err, obj, operation

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
)
