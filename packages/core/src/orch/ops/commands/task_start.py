"""orch task start: take the lease on a task"""

from typing import Any

from orch.ops import plans
from orch.ops._dsl import STR, TASK, err, obj, operation
from orch.ops.base import Context, Result


def handle(ctx: Context, args: dict[str, Any]) -> Result:
    """The ticket is the task's (``DEMO-0043/T3``) or the session's claim; the session must hold the claim."""
    ref = args["task"].rpartition("/")[0] or None
    return plans.run(
        ctx, "task.start", {**args, "ref": ref}, lambda c, p, a: plans.task_state(c, p, a, "task.started"), claim=True
    )


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
    handler=handle,
)
