"""orch doctor: check the workspace's health"""

from typing import Any

from orch.ops._dsl import INT, STR, B, arr, err, obj, operation
from orch.ops.base import Context, Result
from orch.ops.errors import OrchError
from orch.ops.runtime import Call, Workspace

_SHOWN = 20


def _handle(ctx: Context, args: dict[str, Any]) -> Result:
    from orch.instructions import collect_rows, known_worktrees, stale_findings
    from orch.ops import health

    ws = ctx.workspace or Workspace(ctx.env, ctx.now, files=ctx.caps.files)
    root = ws.require_root()
    lines: list[str] = []
    if ctx.hosted:
        # Host mode (H4a1): the host's own store, the viewer's filter, no second store, no config.json, no state
        # directory. --repair is refused by the host before this runs (a control action, not a workspace op).
        if args.get("repair"):
            raise OrchError("internal", "doctor --repair is not served by the host")
        c = Call.of(ctx, "doctor")
        found, _ = health.inspect(
            root, c.store.workspace_id, None, fast=False, caps=ctx.caps, store=c.store, sees=c.sees,
            instructions=stale_findings(
                root,
                c.store.layout.store,
                collect_rows(c.store.layout.store, c.store.state.workspace.addons),
                known_worktrees(ctx.caps),
            ),
        )  # fmt: skip
        return _result(found, lines)
    cfg = ws.config  # as discovery read it; then the root must still be the directory found
    root = ws.verify_root()
    if cfg is None:
        raise OrchError("not_found", "config.json of the workspace is missing or unreadable", hint="orch setup")
    wid = cfg["workspace"]["id"]

    def run() -> list[health.Finding]:
        found, store = health.inspect(
            root,
            wid,
            ws.state_dir,
            fast=False,
            instructions=stale_findings(root, ws.store_dir),
            caps=ctx.caps,
            ident=ws.ident,
            config=ws.config_bytes,
            store_pin=ws.store_pin,
        )
        if store is not None:
            store.close()
        return found

    findings = run()
    if args.get("repair") and not ctx.dry_run:
        did = health.repair(root, wid, ctx.env, findings, ctx.now, ctx.caps)
        lines += [f"repaired: {d}" for d in did]
        findings = run()
    return _result(findings, lines)


def _result(findings: list[Any], lines: list[str]) -> Result:
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
        head=f"doctor: {len(errors)} errors, {len(findings) - len(errors)} warnings" if errors else None,
    )


OP = operation(
    "doctor",
    "Admin",
    "Verify the logs, signatures, checkpoints, pins and files; reports and changes nothing unless --repair, which "
    "only does the safe repairs through the store.",
    who="read",
    props={"repair": B("do the safe repairs: answer external edits, rebuild the index")},
    pre=("workspace_exists",),
    text="ok doctor {problems}\nnext: {next}",
    data=obj({"problems": INT, "findings": arr(obj({"where": STR, "what": STR, "level": STR}))}),
    errors=(err("not_found", "run it in a workspace folder (orch setup makes one)", ["orch", "help"]),),
    handler=_handle,
)
