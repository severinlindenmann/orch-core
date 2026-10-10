"""orch instructions sync: refresh AGENTS.orch.md"""

from typing import Any

from orch.ops._dsl import STR, arr, err, obj, operation
from orch.ops.base import Context, Result
from orch.ops.runtime import Workspace


def handle(ctx: Context, args: dict[str, Any]) -> Result:
    from orch.instructions import INSTRUCTIONS_REV, write_workspace_files

    ws = ctx.workspace or Workspace(ctx.env, ctx.now)
    root = ws.require_root()
    changed = write_workspace_files(root, pointers=False)
    version = f"r{INSTRUCTIONS_REV}"
    lines = [f"wrote {x}" for x in changed] or ["already current"]
    return Result(data={"version": version, "files": changed}, lines=lines, hints=["orch status"])


OP = operation(
    "instructions.sync",
    "Admin",
    "Rewrite AGENTS.orch.md and the built-in skills to the installed version; no grant needed, no event.",
    who="read",
    pre=("workspace_exists",),
    text="ok instructions.sync {version}\nnext: {next}",
    data=obj({"version": STR, "files": arr(STR)}),
    errors=(err("not_found", "run orch init in a workspace", ["orch", "describe", "init"]),),
    handler=handle,
)
