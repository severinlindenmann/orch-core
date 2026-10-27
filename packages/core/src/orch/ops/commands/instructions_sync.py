"""orch instructions sync: refresh AGENTS.orch.md"""

from typing import Any

from orch.ops._dsl import STR, B, arr, err, obj, operation
from orch.ops.base import Context, Result
from orch.ops.errors import OrchError
from orch.ops.runtime import Call, Workspace


def handle(ctx: Context, args: dict[str, Any]) -> Result:
    from orch.instructions import (
        INSTRUCTIONS_REV,
        AddonBlockError,
        UnsafePath,
        collect_rows,
        known_worktrees,
        write_workspace_files,
    )

    ws = ctx.workspace or Workspace(ctx.env, ctx.now, files=ctx.caps.files)
    root = ws.require_root()
    dry = bool(ctx.dry_run or args.get("dry_run"))
    c = Call.of(ctx, "instructions.sync")
    rows = collect_rows(c.store.layout.store, c.store.state.workspace.addons)  # the log's add-ons and their packages
    try:
        with c.store.locked():  # one writer of the generated files at a time (the host and the hook take it too)
            changed, kept = write_workspace_files(
                root,
                pointers=False,
                force=bool(args.get("force")),
                dry_run=dry,
                store=ws.store_dir,
                addons=rows,
                worktrees=known_worktrees(ctx.caps),
            )
    except AddonBlockError as e:
        raise OrchError(
            "invalid.input",
            f"AGENTS.orch.md was not rewritten: {e} (add-on skill folders may already be up to date)",
            hint=e.hint,
        ) from e
    except UnsafePath as e:
        raise OrchError(
            "invalid.input",
            f"stopped: {e} (files before this point may have been written)",
            hint="remove the symbolic link, then retry",
        ) from e
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
    errors=(err("not_found", "run it in a workspace folder (orch setup makes one)", ["orch", "help"]),),
    handler=handle,
)
