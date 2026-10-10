"""orch artifact list: list the stored files"""

from typing import Any

from orch.cli.render import fence
from orch.ops._dsl import INT, REF, STR, arr, err, obj, operation
from orch.ops.base import Context, Result
from orch.ops.runtime import Call


def handle(ctx: Context, args: dict[str, Any]) -> Result:
    c = Call.of(ctx, "artifact.list")
    view = c.resolve(args.get("ref"))
    arts = []
    for a in view.artifacts:
        row = {"name": a.name, "kind": a.kind}
        if a.ac:
            row["ac"] = a.ac
        arts.append(row)
    body = "\n".join(
        f"{a.name} {a.kind}{' ac=' + a.ac if a.ac else ''}{' task=' + a.task if a.task else ''}"
        f"{'' if a.evidence else ' (not evidence)'}"
        for a in view.artifacts
    )
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
    data=obj({"count": INT, "artifacts": arr(obj({"name": STR, "kind": STR, "ac": STR}, optional=("ac",)))}),
    errors=(err("not_found"), err("ambiguous_ref")),
    handler=handle,
)
