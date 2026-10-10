"""orch check: check the ticket files"""

from typing import Any

from orch.ops._dsl import INT, STR, arr, err, obj, operation
from orch.ops.base import Context, Result
from orch.ops.runtime import Workspace, guarded


def _handle(ctx: Context, args: dict[str, Any]) -> Result:
    from orch.instructions import stale_findings

    ws = ctx.workspace or Workspace(ctx.env, ctx.now)
    root = ws.require_root()
    store = ws.store
    findings = [
        {"where": f"{log}#{seq}", "what": f"{code}: {detail}"} for log, seq, code, detail in store.chain_errors()
    ]
    findings += [{"where": r.log or "workspace", "what": f"{r.code}: {r.detail}"} for r in store.reports]
    findings += stale_findings(root)
    shown = findings[:20]
    lines = [f"{f['where']}: {f['what']}" for f in shown]
    if len(findings) > len(shown):
        lines.append(f"+{len(findings) - len(shown)} more")
    stale = any(f["where"].startswith(("AGENTS", ".claude")) for f in findings)
    hint = "orch instructions sync" if stale else ("orch doctor" if findings else "orch status")
    return Result(data={"problems": len(findings), "findings": shown}, lines=lines, hints=[hint])


OP = operation(
    "check",
    "Admin",
    "Check that the ticket files match their events and the instructions are current (for a commit hook).",
    who="read",
    pre=("workspace_exists",),
    text="ok check {problems}\nnext: {next}",
    data=obj({"problems": INT, "findings": arr(obj({"where": STR, "what": STR}))}),
    errors=(err("not_found", "run orch init in a workspace", ["orch", "describe", "init"]),),
    handler=guarded(_handle, "check", []),
)
