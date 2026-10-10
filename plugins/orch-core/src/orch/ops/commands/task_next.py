"""orch task next: the next task to do"""

from typing import Any

from orch.canon.text import clean_line
from orch.ops import plans, views
from orch.ops._dsl import REF_PATTERN, STR, S, arr, err, obj, operation
from orch.ops.base import Context, Result
from orch.ops.runtime import Call


def handle(ctx: Context, args: dict[str, Any]) -> Result:
    c = Call.of(ctx, "task.next")
    view = c.resolve(args.get("ref"))
    c.shown(view, fields=("tasks",))
    t = views.next_task(view, ctx.session)
    if t is None:
        done = bool(view.tasks) and all(x.state in ("done", "skipped") for x in view.tasks)
        return c.result(
            view, {"task": "none"}, hints=["orch submit" if done else "orch task add TEXT"], lines=["no task is open"]
        )
    verify = next((x["verify"] for x in view.fields["tasks"] if x["id"] == t.id), None)
    data: dict[str, Any] = {"task": t.id, "text": clean_line(t.text), "proves": list(t.proves)}
    if verify:
        data["verify"] = clean_line(verify["cmd"])
    hint = (
        f"orch task done {t.id}" + (" --run" if verify else "") if t.state == "started" else f"orch task start {t.id}"
    )
    return c.result(view, data, hints=[hint], lines=plans.render_next_task(view, ctx.session))


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
    handler=handle,
)
