"""orch task list: list the tasks"""

from typing import Any

from orch.ops import verify_trust, views
from orch.ops._dsl import BOOL, INT, REF_PATTERN, STR, S, arr, err, obj, operation
from orch.ops.base import Context, Result
from orch.ops.runtime import Call, flat


def _codebase_repos(c: Call, view: Any) -> list[str]:
    """The repos a verify command runs in: the linked ones if the ticket has a codebase (shared rule), else none."""
    from orch.ops.workdir import has_codebase

    return list(view.fields["links"]["repos"]) if has_codebase(c.store, view) else []


def handle(ctx: Context, args: dict[str, Any]) -> Result:
    c = Call.of(ctx, "task.list")
    view = c.resolve(args.get("ref"))
    c.shown(view, fields=("tasks",))
    tasks = [{"id": t.id, "text": flat(t.text), "state": t.state} for t in view.tasks]
    # for a host client that runs a verify command itself (H3b2): the exact command and the repos the ticket links
    # and whether the caller may run it (F1 "Receipts": an approved plan's command or its own person's), with who wrote
    # it: the client runs nothing before it knows
    cmds = {x["id"]: x["verify"]["cmd"] for x in view.fields["tasks"] if x["verify"] is not None}
    person, grants = verify_trust.caller_person(c), verify_trust.grants(c)

    def verify_of(tid: str) -> dict[str, Any]:
        return {
            "verify": cmds[tid],
            "verify_runnable": verify_trust.trust(view, tid, person, grants) is not None,
            "verify_author": verify_trust.author(view, tid) or "unknown",
        }

    rows = [{**t, **verify_of(t["id"])} if t["id"] in cmds else t for t in tasks]
    return c.result(
        view,
        {"count": len(rows), "tasks": rows, "repos": _codebase_repos(c, view)},
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
    data=obj(
        {
            "count": INT,
            "tasks": arr(
                obj(
                    {
                        "id": STR,
                        "text": STR,
                        "state": STR,
                        "verify": STR,
                        "verify_runnable": BOOL,
                        "verify_author": STR,
                    },
                    optional=("verify", "verify_runnable", "verify_author"),
                )
            ),
            "repos": arr(STR),
        }
    ),
    errors=(err("not_found"), err("ambiguous_ref")),
    handler=handle,
)
