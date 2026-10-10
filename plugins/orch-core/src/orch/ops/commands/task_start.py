"""orch task start: take the lease on a task"""

from orch.ops._dsl import STR, TASK, err, obj, operation

OP = operation(
    "task.start",
    "Edit",
    "Take the lease on one task (parallel subagents use ORCH_SESSION=<parent>.<n>).",
    who="agent",
    props={"task": TASK()},
    required=("task",),
    positional=("task",),
    pre=("ticket_exists", "session_holds_claim", "task_exists", "task_lease_free", "grant_valid"),
    emits=("task.started",),
    text="ok {key} task.started {task} seq={seq}\nnext: {next}",
    data=obj({"task": STR}),
    errors=(err("lease.held"), err("claim.required"), err("not_found"), err("transition.refused")),
)
