"""orch import v1: import a v1 workspace"""

from typing import Any

from orch.ops._dsl import INT, S, err, obj, operation
from orch.ops.base import Context, Result


def handle(ctx: Context, args: dict[str, Any]) -> Result:
    from orch.ops.import_run import run_import  # lazy: declaring the operation must not import the importer

    return run_import(ctx, args)


OP = operation(
    "import.v1",
    "Admin",
    "Import a v1 workspace: its tickets, with the v1 history and files; asks for your passphrase once.",
    who="human",
    props={"path": S("the v1 workspace (the project folder or its orchestrator folder)", **{"x-metavar": "PATH"})},
    required=("path",),
    positional=("path",),
    pre=("workspace_exists", "v1_workspace", "user_presence", "text_clean"),
    emits=(
        "artifact.added",
        "log.added",
        "question.asked",
        "status.changed",
        "ticket.closed",
        "ticket.created",
        "ticket.updated",
    ),
    text="ok import.v1 {count} tickets ({events} events)\nnext: {next}",
    data=obj({"count": INT, "already": INT, "skipped": INT, "events": INT, "partial": INT}),
    errors=(err("not_found"), err("parse.text")),
    handler=handle,
)
