"""orch task next: the next task to do"""

from orch.ops._dsl import REF_PATTERN, STR, S, arr, err, obj, operation

OP = operation(
    "task.next",
    "Edit",
    "The next task to do: its text, what it proves and its verify command.",
    who="read",
    props={"ref": S("ticket REF (flag); default: your claim", pattern=REF_PATTERN, **{"x-metavar": "REF"})},
    pre=("ticket_exists", "ticket_visible"),
    text="ok {key} task.next {task}\nnext: {next}",
    data=obj(
        {"task": STR, "text": STR, "proves": arr(STR), "verify": STR}, optional=("task", "text", "proves", "verify")
    ),
    errors=(err("not_found"), err("ambiguous_ref")),
)
