"""orch doctor: check the workspace's health"""

from typing import Any

from orch.ops._dsl import INT, STR, B, arr, obj, operation
from orch.ops.base import Context, Result
from orch.ops.runtime import Workspace, _config

_SHOWN = 20


def _handle(ctx: Context, args: dict[str, Any]) -> Result:
    from orch.instructions import stale_findings
    from orch.ops import health

    ws = ctx.workspace or Workspace(ctx.env, ctx.now)
    root = ws.require_root()
    wid = (_config(root) or {})["workspace"]["id"]
    lines: list[str] = []

    def run() -> list[health.Finding]:
        found, store = health.inspect(root, wid, ws.state_dir, fast=False, instructions=stale_findings(root))
        if store is not None:
            store.close()
        return found

    findings = run()
    if args.get("repair") and not ctx.dry_run:
        did = health.repair(root, wid, ctx.env, findings, ctx.now)
        lines += [f"repaired: {d}" for d in did]
        findings = run()
    errors = [f for f in findings if f.level == "error"]
    shown = sorted(findings, key=lambda f: f.level != "error")[:_SHOWN]
    lines += [f"{f.level} {f.where}: {f.code}: {f.what}" for f in shown]
    if len(findings) > len(shown):
        lines.append(f"+{len(findings) - len(shown)} more")
    repairable = any(f.repair for f in findings)
    hint = (
        "orch doctor --repair"
        if repairable
        else ("an owner-signed restore repairs a broken chain" if errors else "orch status")
    )
    return Result(
        data={
            "problems": len(findings),
            "findings": [{"where": f.where, "what": f"{f.code}: {f.what}", "level": f.level} for f in shown],
        },
        lines=lines,
        hints=[hint],
        exit=5 if errors else 0,
    )


OP = operation(
    "doctor",
    "Admin",
    "Verify the logs, signatures, checkpoints, pins and files; reports and changes nothing unless --repair, which "
    "only does the safe repairs through the store.",
    who="read",
    props={"repair": B("do the safe repairs: answer external edits, rebuild the index, drop dead init keys")},
    pre=("workspace_exists",),
    text="ok doctor {problems}\nnext: {next}",
    data=obj({"problems": INT, "findings": arr(obj({"where": STR, "what": STR, "level": STR}))}),
    handler=_handle,
)
