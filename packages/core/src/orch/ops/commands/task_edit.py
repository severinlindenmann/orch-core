"""orch task edit: change a task's text, verify command or the criteria it proves"""

from typing import Any

from orch.ops import plans
from orch.ops._dsl import AC_PATTERN, REF_PATTERN, STR, TASK, L, S, err, obj, operation
from orch.ops.base import Context, Result


def handle(ctx: Context, args: dict[str, Any]) -> Result:
    """The ticket is the task's (``DEMO-0043/T3``) or ``--ref``/the session's claim."""
    ref = args["task"].rpartition("/")[0] or args.get("ref") or None
    return plans.run(ctx, "task.edit", {**args, "ref": ref}, plans.task_edit)


OP = operation(
    "task.edit",
    "Edit",
    "Change a task's text, its verify command or the criteria it proves. A task that is done keeps its verify "
    "command and its criteria.",
    who="agent",
    props={
        "task": TASK(),
        "text": S("the new text of the task", **{"x-metavar": "TEXT"}),
        "verify": S("the new verify command; an empty value removes it", **{"x-metavar": "CMD"}),
        "proves": L(
            "the criteria it proves, comma separated (through the host API an empty list proves none)",
            split=True,
            items={"type": "string", "pattern": AC_PATTERN},
            **{"x-metavar": "AC1,AC2"},
        ),
        "ref": S("ticket REF (flag)", pattern=REF_PATTERN, **{"x-metavar": "REF"}),
    },
    required=("task",),
    positional=("task",),
    pre=(
        "ticket_exists",
        "ticket_open_for_work",
        "task_exists",
        "acceptance_exists",
        "base_rev_tracked",
        "grant_valid",
        "text_clean",
    ),
    emits=("ticket.updated",),
    text="ok {key} ticket.updated {task} seq={seq}\nnext: {next}",
    data=obj({"task": STR}),
    errors=(err("conflict.field"), err("transition.refused"), err("not_found"), err("parse.text")),
    handler=handle,
)
