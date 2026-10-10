"""orch task list: list the tasks"""

from typing import Any

from orch.canon.text import clean_line
from orch.ops import views
from orch.ops._dsl import INT, REF_PATTERN, STR, S, arr, err, obj, operation
from orch.ops.base import Context, Result
from orch.ops.runtime import Call


def handle(ctx: Context, args: dict[str, Any]) -> Result:
    c = Call.of(ctx, "task.list")
    view = c.resolve(args.get("ref"))
    c.shown(view, fields=("tasks",))
    tasks = [{"id": t.id, "text": clean_line(t.text), "state": t.state} for t in view.tasks]
    return c.result(
        view,
        {"count": len(tasks), "tasks": tasks},
        hints=[views.next_hint(view, ctx.session)],
        lines=views.fenced_tasks(view, f"tasks of {view.key}", 100) if tasks else ["no tasks"],
    )


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
    handler=handle,
)
