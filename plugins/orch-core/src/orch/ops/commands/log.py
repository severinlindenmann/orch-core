"""orch log: add a note to the ticket log"""

from orch.ops._dsl import REF_PATTERN, S, err, obj, operation

OP = operation(
    "log",
    "Edit",
    "Add a note to the ticket log; works without a grant (unattended).",
    who="unattended",
    props={
        "text": S("the note, at most 4096 bytes", **{"x-metavar": "TEXT"}),
        "ref": S("ticket REF (flag); default: your claim", pattern=REF_PATTERN, **{"x-metavar": "REF"}),
    },
    required=("text",),
    positional=("text",),
    pre=("ticket_exists", "ticket_visible", "text_clean", "unattended_quota"),
    emits=("log.added",),
    text="ok {key} log.added seq={seq}\nnext: {next}",
    data=obj({}),
    errors=(err("quota.unattended"), err("parse.text"), err("not_found"), err("ambiguous_ref")),
)
