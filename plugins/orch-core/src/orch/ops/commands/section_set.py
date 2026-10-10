"""orch section set: replace one body section"""

from orch.ops._dsl import FILE, MSG, REF_PATTERN, STR, E, S, err, obj, operation

OP = operation(
    "section.set",
    "Edit",
    "Replace one section of the body with text from -m or --file.",
    who="agent",
    props={
        "section": E(
            "section id",
            "summary",
            "context",
            "requirements",
            "out_of_scope",
            "plan",
            "decisions",
            "verification",
            "findings",
            "current_state",
            **{"x-metavar": "SECTION"},
        ),
        "ref": S("ticket REF (flag); default: your claim", pattern=REF_PATTERN, **{"x-metavar": "REF"}),
        "message": MSG,
        "file": FILE,
    },
    required=("section",),
    positional=("section",),
    one_of=("message", "file"),
    pre=("ticket_exists", "ticket_open_for_work", "base_rev_tracked", "grant_valid", "text_clean"),
    emits=("ticket.updated",),
    text="ok {key} ticket.updated {section} seq={seq}\nnext: {next}",
    data=obj({"section": STR}),
    errors=(err("conflict.section"), err("transition.refused"), err("not_found"), err("parse.text")),
)
