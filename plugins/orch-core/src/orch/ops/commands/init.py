"""orch init: create a workspace"""

from typing import Any

from orch.ops._dsl import STR, B, S, err, obj, operation
from orch.ops.base import Context, Result


def handle(ctx: Context, args: dict[str, Any]) -> Result:
    from orch.ops.workspace_init import create_workspace, create_workspace_hosted

    if args.get("host"):
        return create_workspace_hosted(ctx, args)
    return create_workspace(ctx, args)


OP = operation(
    "init",
    "Admin",
    "Create a workspace here: the genesis event, the workspace key and the instructions.",
    who="human",
    props={
        "prefix": S("ticket key prefix, for example DEMO", pattern=r"^[A-Z][A-Z0-9]{0,15}$", **{"x-metavar": "PREFIX"}),
        "name": S("your display name", **{"x-metavar": "NAME"}),
        "host": B(
            "create it through the running orch host (host.wsk_create, then host.bootstrap): the host keeps the "
            "workspace key and serves the workspace; the directory must be empty"
        ),
    },
    required=("prefix",),
    pre=("no_workspace", "user_presence", "text_clean"),
    emits=("workspace.created",),
    text="ok init {prefix} seq={seq}\nnext: {next}",
    data=obj({"prefix": STR, "workspace_id": STR}),
    errors=(
        err("parse.text"),
        err("human_only", "run orch init in your own terminal, not through an agent", ["orch", "describe", "init"]),
        err("host.busy"),
        err("custody.unsafe_mode"),
        err("host.unavailable"),
        err("host.dev_refused"),  # orch-v2-host 11A.17: the host refuses a dev-pair sandbox (or a dev host a real one)
    ),
    handler=handle,
)
