"""orch task reopen: reopen a finished, skipped or blocked task"""

from orch.ops._dsl import STR, TASK, S, err, obj, operation

OP = operation(
    "task.reopen",
    "Edit",
    "Reopen a finished, skipped or blocked task.",
    who="agent",
    props={"task": TASK(), "reason": S("why", **{"x-metavar": "TEXT"})},
    required=("task",),
    positional=("task",),
    pre=("ticket_exists", "session_holds_claim", "task_exists", "session_holds_lease", "grant_valid", "text_clean"),
    emits=("task.reopened",),
    text="ok {key} task.reopened {task} seq={seq}\nnext: {next}",
    data=obj({"task": STR}),
    errors=(err("lease.required"), err("claim.required"), err("not_found"), err("transition.refused")),
)
