"""orch ask: ask a person and carry on"""

from orch.ops._dsl import BOOL, REF_PATTERN, STR, B, L, S, err, obj, operation

OP = operation(
    "ask",
    "Lifecycle",
    "Ask a person; works without a grant (unattended). Then orch wait.",
    who="unattended",
    props={
        "text": S("the question", **{"x-metavar": "TEXT"}),
        "ref": S("ticket REF (flag); default: your claim", pattern=REF_PATTERN, **{"x-metavar": "REF"}),
        "options": L("answer options, comma separated keys", **{"x-metavar": "A,B"}),
        "rec": S("the recommended option key", **{"x-metavar": "KEY"}),
        "to": S("person id or role to ask", **{"x-metavar": "WHO"}),
        "why": S("why it matters", **{"x-metavar": "TEXT"}),
        "non_blocking": B("work can go on while it is open"),
    },
    required=("text",),
    positional=("text",),
    pre=("ticket_exists", "ticket_visible", "text_clean", "unattended_quota"),
    emits=("question.asked",),
    text="ok {key} question.asked {question} seq={seq}\nnext: {next}",
    data=obj({"question": STR, "blocking": BOOL}),
    errors=(err("quota.unattended"), err("parse.text"), err("not_found"), err("ambiguous_ref")),
)
