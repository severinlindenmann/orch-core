"""orch submit: move the ticket to testing"""

from orch.ops._dsl import REF, STR, err, obj, operation

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
)
