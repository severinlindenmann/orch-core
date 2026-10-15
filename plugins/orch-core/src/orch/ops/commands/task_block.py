"""orch task block: mark a task blocked, with a reason"""

from typing import Any

from orch.ops import plans
from orch.ops._dsl import STR, TASK, S, err, obj, operation
from orch.ops.base import Context, Result


def handle(ctx: Context, args: dict[str, Any]) -> Result:
    """The ticket is the task's (``DEMO-0043/T3``) or the session's claim; the session must hold the claim."""
    ref = args["task"].rpartition("/")[0] or None
    return plans.run(
        ctx, "task.block", {**args, "ref": ref}, lambda c, p, a: plans.task_state(c, p, a, "task.blocked"), claim=True
    )


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
    errors=(
        err("lease.required"),
        err("lease.held"),
        err("claim.required"),
        err("not_found"),
        err("transition.refused"),
    ),
    handler=handle,
)
