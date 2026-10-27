"""orch check: check the ticket files"""

from typing import Any

from orch.ops._dsl import INT, STR, B, arr, err, obj, operation
from orch.ops.base import Context, Result
from orch.ops.errors import OrchError
from orch.ops.runtime import Call, Workspace

_SHOWN = 20


def what(f: Any) -> str:
    return f.what if f.code == "instructions.stale" else f"{f.code}: {f.what}"


def _handle(ctx: Context, args: dict[str, Any]) -> Result:
    from orch.instructions import collect_rows, known_worktrees, stale_findings
    from orch.ops import health

    ws = ctx.workspace or Workspace(ctx.env, ctx.now, files=ctx.caps.files)
    root = ws.require_root()
    if ctx.hosted:
        # Host mode (H4a1): the host's own store, filtered for the viewer before anything is counted (F6); no second
        # store, no config.json. --staged needs git, which the host's handlers do not have (problem line).
        c = Call.of(ctx, "check")
        findings, _ = health.inspect(
            root, c.store.workspace_id, None, fast=True, caps=ctx.caps, store=c.store, sees=c.sees,
            instructions=stale_findings(
                root,
                c.store.layout.store,
                collect_rows(c.store.layout.store, c.store.state.workspace.addons),
                known_worktrees(ctx.caps),
            ),
        )  # fmt: skip
        if args.get("staged"):
            findings += health.check_staged(c.store, root, ctx.caps)
    else:
        cfg = ws.config  # as discovery read it; then the root must still be the directory found
        root = ws.verify_root()
        if cfg is None:
            raise OrchError("not_found", "config.json of the workspace is missing or unreadable", hint="orch setup")
        wid = cfg["workspace"]["id"]
        findings, store = health.inspect(
            root,
            wid,
            ws.state_dir,
            fast=True,
            instructions=stale_findings(root, ws.store_dir),
            caps=ctx.caps,
            ident=ws.ident,
            config=ws.config_bytes,
            store_pin=ws.store_pin,
        )
        try:
            if args.get("staged") and store is not None:
                findings += health.check_staged(store, root, ctx.caps)
        finally:
            if store is not None:
                store.close()
    shown = findings[:_SHOWN]
    lines = [f"{f.where}: {what(f)}" for f in shown]
    if len(findings) > len(shown):
        lines.append(f"+{len(findings) - len(shown)} more")
    stale = any(f.code == "instructions.stale" for f in findings)
    hint = (
        (
            "orch instructions sync --force"
            if any("edited by hand" in f.what for f in findings)
            else "orch instructions sync"
        )
        if stale
        else ("orch doctor" if findings else "orch status")
    )
    return Result(
        data={
            "problems": len(findings),
            "findings": [{"where": f.where, "what": what(f)} for f in shown],
        },
        lines=lines,
        hints=[hint],
        exit=5 if findings else 0,
        head=f"check: {len(findings)} problems" if findings else None,
    )


OP = operation(
    "check",
    "Admin",
    "Check that the ticket files match their events and the instructions are current (for a commit hook or CI); "
    "--staged also checks what is about to be committed.",
    who="read",
    props={"staged": B("also check the staged files: hand edits, forged log lines, .state, secrets")},
    pre=("workspace_exists",),
    text="ok check {problems}\nnext: {next}",
    data=obj({"problems": INT, "findings": arr(obj({"where": STR, "what": STR}))}),
    errors=(err("not_found", "run it in a workspace folder (orch setup makes one)", ["orch", "help"]),),
    handler=_handle,
)
