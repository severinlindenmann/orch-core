"""orch show: read a ticket, a section, the log or a diff"""

from orch.ops._dsl import REF, STR, B, I, L, err, obj, operation

OP = operation(
    "show",
    "Read",
    "Read a ticket: the default view, some sections, everything, the log or a diff since an event.",
    who="read",
    props={
        "ref": REF(),
        "section": L("only these sections, comma separated", **{"x-metavar": "A,B"}),
        "full": B("every section"),
        "log": B("the event log"),
        "diff": B("what changed"),
        "since": I("events after this seq", minimum=0, **{"x-metavar": "N"}),
    },
    positional=("ref",),
    pre=("ticket_exists", "ticket_visible"),
    text="ok {key} show {view} seq={seq}\nnext: {next}",
    data=obj({"view": STR, "ticket": {"type": "object"}}, optional=("ticket",)),
    errors=(err("not_found"), err("ambiguous_ref")),
)
