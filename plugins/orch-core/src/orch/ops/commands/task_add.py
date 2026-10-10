"""orch task add: add a task"""

from orch.ops._dsl import AC_PATTERN, REF_PATTERN, STR, L, S, err, obj, operation

OP = operation(
    "task.add",
    "Edit",
    "Add a task, optionally with a verify command and the criteria it proves.",
    who="agent",
    props={
        "text": S("the task", **{"x-metavar": "TEXT"}),
        "verify": S("verify command", **{"x-metavar": "CMD"}),
        "proves": L(
            "criteria it proves, comma separated",
            split=True,
            items={"type": "string", "pattern": AC_PATTERN},
            **{"x-metavar": "AC1,AC2"},
        ),
        "assignee": S("person id", **{"x-metavar": "PERSON"}),
        "ref": S("ticket REF (flag)", pattern=REF_PATTERN, **{"x-metavar": "REF"}),
    },
    required=("text",),
    positional=("text",),
    pre=("ticket_exists", "ticket_open_for_work", "acceptance_exists", "base_rev_tracked", "grant_valid", "text_clean"),
    emits=("ticket.updated",),
    text="ok {key} ticket.updated {task} seq={seq}\nnext: {next}",
    data=obj({"task": STR}),
    errors=(err("conflict.field"), err("transition.refused"), err("not_found"), err("parse.text")),
)
