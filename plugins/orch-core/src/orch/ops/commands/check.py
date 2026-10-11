"""orch check: check the ticket files"""

from typing import Any

from orch.ops._dsl import INT, STR, B, arr, err, obj, operation
from orch.ops.base import Context, Result
from orch.ops.errors import OrchError
from orch.ops.runtime import Workspace, _config

_SHOWN = 20


def what(f: Any) -> str:
    return f.what if f.code == "instructions.stale" else f"{f.code}: {f.what}"


def _handle(ctx: Context, args: dict[str, Any]) -> Result:
    from orch.instructions import stale_findings
    from orch.ops import health

    ws = ctx.workspace or Workspace(ctx.env, ctx.now)
    root = ws.require_root()
    cfg = _config(root)
    if cfg is None:
        raise OrchError("not_found", "config.json of the workspace is missing or unreadable", hint="orch init")
    wid = cfg["workspace"]["id"]
    findings, store = health.inspect(root, wid, ws.state_dir, fast=True, instructions=stale_findings(root))
    try:
        if args.get("staged") and store is not None:
            findings += health.check_staged(store, root)
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
    errors=(err("not_found", "run orch init in a workspace", ["orch", "describe", "init"]),),
    handler=_handle,
)
