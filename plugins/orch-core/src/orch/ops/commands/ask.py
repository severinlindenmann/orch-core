"""orch ask: ask a person and carry on"""

from typing import Any

from orch.ops import plans
from orch.ops._dsl import BOOL, REF_PATTERN, STR, B, L, S, err, obj, operation
from orch.ops.base import Context, Result


def handle(ctx: Context, args: dict[str, Any]) -> Result:
    return plans.run(ctx, "ask", args, plans.ask)


OP = operation(
    "ask",
    "Lifecycle",
    "Ask a person; works without a grant (unattended). Then orch wait.",
    who="unattended",
    props={
        "text": S("the question (up to 140 characters)", **{"x-metavar": "TEXT"}),
        "ref": S("ticket REF (flag); default: your claim", pattern=REF_PATTERN, **{"x-metavar": "REF"}),
        "option": L(
            "an answer: 'key | title | text' (2 to 6, text is optional)", **{"x-metavar": "'KEY | TITLE | TEXT'"}
        ),
        "fact": L(
            "a fact of an option: 'key | Label | value' (1 to 3 per option)", **{"x-metavar": "'KEY | LABEL | VALUE'"}
        ),
        "rec": S("the recommended option key", **{"x-metavar": "KEY"}),
        "allow_other": B("the person may also type an answer of their own"),
        "details": S("more detail for the person (up to 1500 characters)", **{"x-metavar": "TEXT"}),
        "details_file": S("read the details from PATH, or - for stdin", **{"x-metavar": "PATH", "x-file": True}),
        "text_question": B("an open question: the person types the answer, no options"),
        "question_file": S(
            "the whole question as JSON, from PATH or - for stdin (not with TEXT or the flags above)",
            **{"x-metavar": "PATH", "x-file": True},
        ),
        "to": S("person id or role to ask", **{"x-metavar": "WHO"}),
        "why": S("why it matters", **{"x-metavar": "TEXT"}),
        "non_blocking": B("work can go on while it is open"),
    },
    positional=("text",),
    one_of=("text", "question_file"),
    pre=("ticket_exists", "ticket_visible", "unattended_scope", "text_clean", "unattended_quota"),
    emits=("question.asked",),
    text="ok {key} question.asked {question} seq={seq}\nnext: {next}",
    data=obj({"question": STR, "blocking": BOOL}),
    errors=(err("quota.unattended"), err("parse.text"), err("not_found"), err("ambiguous_ref")),
    handler=handle,
)
