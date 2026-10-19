"""orch artifact list: list the stored files"""

from typing import Any

from orch.ops._dsl import INT, REF, STR, arr, err, obj, operation
from orch.ops.base import Context, Result
from orch.ops.runtime import Call
from orch.ops.views import fence


def handle(ctx: Context, args: dict[str, Any]) -> Result:
    c = Call.of(ctx, "artifact.list")
    view = c.resolve(args.get("ref"))
    store, root = c.ws.store_dir, c.ws.root
    base = None
    if store is not None and root is not None:
        try:  # relative to the project, so the same text everywhere (an agent in a worktree adds its project folder)
            base = store.relative_to(root)
        except ValueError:
            base = None
    arts = []
    lines = []
    for a in view.artifacts:
        row = {"name": a.name, "kind": a.kind}
        if a.ac:
            row["ac"] = a.ac
        # R3 part B: where an agent can open the file (read only: the log names its digest, a changed file is not truth)
        path = str(base / "tickets" / view.uid / "artifacts" / a.name) if base is not None and a.digest else None
        if path:
            row["path"] = path
        arts.append(row)
        lines.append(
            f"{a.name} {a.kind}{' ac=' + a.ac if a.ac else ''}{' task=' + a.task if a.task else ''}"
            f"{'' if a.evidence else ' (not evidence)'}{' path=' + path if path else ''}"
        )
    body = "\n".join(lines)
    return c.result(
        view,
        {"count": len(arts), "artifacts": arts},
        hints=["orch show"],
        lines=fence(body, f"artifacts of {view.key}") if arts else ["no artifacts"],
    )


OP = operation(
    "artifact.list",
    "Edit",
    "List the ticket's artifacts.",
    who="read",
    props={"ref": REF()},
    positional=("ref",),
    pre=("ticket_exists", "ticket_visible"),
    text="ok {key} artifact.list {count}\nnext: {next}",
    data=obj(
        {
            "count": INT,
            "artifacts": arr(obj({"name": STR, "kind": STR, "ac": STR, "path": STR}, optional=("ac", "path"))),
        }
    ),
    errors=(err("not_found"), err("ambiguous_ref")),
    handler=handle,
)
