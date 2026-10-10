"""orch task list: list the tasks"""

from orch.ops._dsl import INT, REF_PATTERN, STR, S, arr, err, obj, operation

OP = operation(
    "task.list",
    "Edit",
    "List the ticket's tasks with their state.",
    who="read",
    props={"ref": S("ticket REF (flag); default: your claim", pattern=REF_PATTERN, **{"x-metavar": "REF"})},
    pre=("ticket_exists", "ticket_visible"),
    text="ok {key} task.list {count}\nnext: {next}",
    data=obj({"count": INT, "tasks": arr(obj({"id": STR, "text": STR, "state": STR}))}),
    errors=(err("not_found"), err("ambiguous_ref")),
)
