"""orch init: create a workspace"""

from orch.ops._dsl import STR, S, err, obj, operation

OP = operation(
    "init",
    "Admin",
    "Create a workspace here: the genesis event, the workspace key and the instructions.",
    who="human",
    props={
        "prefix": S("ticket key prefix, for example DEMO", pattern=r"^[A-Z][A-Z0-9]{0,15}$", **{"x-metavar": "PREFIX"}),
        "name": S("your display name", **{"x-metavar": "NAME"}),
    },
    required=("prefix",),
    pre=("no_workspace", "user_presence", "text_clean"),
    emits=("workspace.created",),
    text="ok init {prefix} seq={seq}\nnext: {next}",
    data=obj({"prefix": STR, "workspace_id": STR}),
    errors=(err("parse.text"),),
)
