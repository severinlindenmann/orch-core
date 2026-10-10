"""orch claim: take the ticket's claim"""

from orch.ops._dsl import BOOL, REF, STR, B, S, err, obj, operation

OP = operation(
    "claim",
    "Lifecycle",
    "Claim a ticket, the next one, or take over another session's claim.",
    who="agent",
    props={
        "ref": REF("ticket REF; default: the next ticket"),
        "next": B("claim the next ticket"),
        "takeover": B("take the claim from another session (needs --reason)"),
        "reason": S("why you take over", **{"x-metavar": "TEXT"}),
    },
    positional=("ref",),
    pre=("ticket_exists", "ticket_visible", "ticket_open_for_work", "claim_free_or_takeover", "grant_valid"),
    emits=("claim.taken",),
    text="ok {key} claim.taken {status} seq={seq}\nnext: {next}",
    data=obj({"status": STR, "takeover": BOOL}, optional=("takeover",)),
    errors=(err("claim.held"), err("not_found"), err("transition.refused")),
)
