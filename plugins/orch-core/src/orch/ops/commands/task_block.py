"""orch task block: mark a task blocked, with a reason"""

from orch.ops._dsl import STR, TASK, S, err, obj, operation

OP = operation(
    "task.block",
    "Edit",
    "Mark a task blocked, with a reason.",
    who="agent",
    props={"task": TASK(), "reason": S("why", **{"x-metavar": "TEXT"})},
    required=("task", "reason"),
    positional=("task",),
    pre=("ticket_exists", "session_holds_claim", "task_exists", "session_holds_lease", "grant_valid", "text_clean"),
    emits=("task.blocked",),
    text="ok {key} task.blocked {task} seq={seq}\nnext: {next}",
    data=obj({"task": STR}),
    errors=(err("lease.required"), err("claim.required"), err("not_found"), err("transition.refused")),
)
