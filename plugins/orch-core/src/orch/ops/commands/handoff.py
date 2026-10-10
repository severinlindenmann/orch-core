"""orch handoff: write where it stands and release the claim"""

from orch.ops._dsl import FILE, INT, MSG, REF, err, obj, operation

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
)
