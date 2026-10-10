"""orch instructions sync: refresh AGENTS.orch.md"""

from typing import Any

from orch.ops._dsl import STR, B, arr, err, obj, operation
from orch.ops.base import Context, Result
from orch.ops.errors import OrchError
from orch.ops.runtime import Workspace


def handle(ctx: Context, args: dict[str, Any]) -> Result:
    from orch.instructions import INSTRUCTIONS_REV, UnsafePath, write_workspace_files

    ws = ctx.workspace or Workspace(ctx.env, ctx.now)
    root = ws.require_root()
    dry = bool(ctx.dry_run or args.get("dry_run"))
    try:
        changed, kept = write_workspace_files(root, pointers=False, force=bool(args.get("force")), dry_run=dry)
    except UnsafePath as e:
        raise OrchError("invalid.input", f"nothing written: {e}", hint="remove the symbolic link, then retry") from e
    verb = "would write" if dry else "wrote"
    lines = [f"{verb} {x}" for x in changed] + kept
    if not lines:
        lines = ["already current"]
    return Result(data={"version": f"r{INSTRUCTIONS_REV}", "files": changed}, lines=lines, hints=["orch status"])


OP = operation(
    "instructions.sync",
    "Admin",
    "Rewrite AGENTS.orch.md and the built-in skills to the installed version; no grant needed, no event.",
    who="read",
    props={
        "dry_run": B("say what would be written, write nothing"),
        "force": B("overwrite a built-in skill that was edited by hand"),
    },
    pre=("workspace_exists",),
    text="ok instructions.sync {version}\nnext: {next}",
    data=obj({"version": STR, "files": arr(STR)}),
    errors=(err("not_found", "run orch init in a workspace", ["orch", "describe", "init"]),),
    handler=handle,
)
