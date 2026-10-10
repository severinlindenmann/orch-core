"""orch import v1: import a v1 workspace"""

from orch.ops._dsl import INT, S, err, obj, operation

OP = operation(
    "import.v1",
    "Admin",
    "Import a v1 workspace: tickets, history as imported events, artifacts.",
    who="human",
    props={"path": S("the v1 workspace", **{"x-metavar": "PATH"})},
    required=("path",),
    positional=("path",),
    pre=("workspace_exists", "v1_workspace", "user_presence"),
    emits=("ticket.created", "log.added"),
    text="ok import.v1 {count} tickets\nnext: {next}",
    data=obj({"count": INT, "skipped": INT}),
    errors=(err("not_found"), err("parse.json")),
)
