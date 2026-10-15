"""orch ask: ask a person and carry on"""

from typing import Any

from orch.ops import plans
from orch.ops._dsl import BOOL, REF_PATTERN, STR, TOKEN_PATTERN, B, L, S, err, obj, operation
from orch.ops.base import Context, Result


def handle(ctx: Context, args: dict[str, Any]) -> Result:
    return plans.run(ctx, "ask", args, plans.ask)


OP = operation(
    "ask",
    "Lifecycle",
    "Ask a person; works without a grant (unattended). Then orch wait.",
    who="unattended",
    props={
        "text": S("the question", **{"x-metavar": "TEXT"}),
        "ref": S("ticket REF (flag); default: your claim", pattern=REF_PATTERN, **{"x-metavar": "REF"}),
        "options": L(
            "answer option keys, comma separated",
            split=True,
            items={"type": "string", "pattern": TOKEN_PATTERN},
            **{"x-metavar": "A,B"},
        ),
        "rec": S("the recommended option key", **{"x-metavar": "KEY"}),
        "to": S("person id or role to ask", **{"x-metavar": "WHO"}),
        "why": S("why it matters", **{"x-metavar": "TEXT"}),
        "non_blocking": B("work can go on while it is open"),
    },
    required=("text",),
    positional=("text",),
    pre=("ticket_exists", "ticket_visible", "unattended_scope", "text_clean", "unattended_quota"),
    emits=("question.asked",),
    text="ok {key} question.asked {question} seq={seq}\nnext: {next}",
    data=obj({"question": STR, "blocking": BOOL}),
    errors=(err("quota.unattended"), err("parse.text"), err("not_found"), err("ambiguous_ref")),
    handler=handle,
)
