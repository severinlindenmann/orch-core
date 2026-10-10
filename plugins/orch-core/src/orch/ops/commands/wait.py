"""orch wait: wait for a decision"""

from orch.ops._dsl import REF_PATTERN, B, I, S, err, operation

OP = operation(
    "wait",
    "Lifecycle",
    "Wait for an answer, an approval, a change request, a verdict or an invalidation (default 540 s).",
    who="read",
    props={
        "ref": S("ticket REF (flag); default: your claim", pattern=REF_PATTERN, **{"x-metavar": "REF"}),
        "timeout": I("seconds to wait", minimum=1, maximum=3600, default=540, **{"x-metavar": "SECONDS"}),
        "strict_timeout": B("exit 7 when the timeout passes"),
    },
    pre=("ticket_exists", "ticket_visible"),
    text="ok {key} wait {kind} cursor={cursor}\nnext: {next}",
    output_ref="https://schemas.orch.dev/v2/wait-result",
    errors=(err("wait.timeout"), err("not_found"), err("ambiguous_ref")),
)
