"""`orch permit …` (AI Factory, #2): permission requests in a factory epic and the human's signed answers.
Agents may `request` and `list`; `grant`, `deny` and `revoke` are the human's; `hook` is Claude Code's
PermissionRequest hook (orch.core.permits)."""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Annotated

import typer

permit_app = typer.Typer(no_args_is_help=True, help="AI Factory permissions: requests and the human's grants.")

JsonOpt = Annotated[bool, typer.Option("--json", help="Machine-readable output.")]


def _ctx():
    from orch import cli
    return cli, cli._ws()


@permit_app.command("request")
def request(command: Annotated[str, typer.Argument(help="The exact command, as it will run.")],
            ticket: Annotated[str, typer.Option("--ticket", help="The factory ticket the command is for.")],
            reason: Annotated[str, typer.Option("--reason", help="Why the work needs it.")] = "",
            json_out: JsonOpt = False) -> None:
    """Agents: ask the human for a permission you do not hold (for example after an auto-mode denial). Signs
    nothing; the human answers it."""
    from orch.actor import cli_actor
    from orch.core import permits, store
    cli, ws = _ctx()
    t = store.load(ws, ticket)[1]
    r = permits.request(ws, cli_actor(), t, command, reason=reason)
    cli._out(r, json_out, f"{r['id']}: waiting for the human's answer; go on with other work or run "
                          f"`orch wait {t.id}`")


@permit_app.command("list")
def list_(json_out: JsonOpt = False) -> None:
    """Open requests and every grant with its state."""
    from orch.core import permits
    from orch.textsafe import visible
    cli, ws = _ctx()
    reqs, grants = permits.open_requests(ws), permits.grants(ws)
    lines = [f"{r['id']:<7} {r['epic']} {r['ticket']} asks: {visible(r['command'])}" for r in reqs] or ["no open requests"]
    lines += [f"grant {g['grant']} ({g['scope']}) {g['epic']}: {visible(str(g['command']))}"
              + ("" if g["live"] else f" · {g['why']}") for g in grants]
    cli._out({"requests": reqs, "grants": grants}, json_out, "\n".join(lines))


def _show_request(ws, rid: str, verb: str) -> dict:
    from orch.core import permits
    from orch.errors import NotFoundError, ValidationError
    from orch.textsafe import decodes_to_hidden, visible
    r = permits.requests(ws).get(rid.upper())
    if r is None:
        raise NotFoundError(f"no permission request {rid}")
    if decodes_to_hidden(r["command"]):
        raise ValidationError(f"{r['id']}: the command holds hidden or control characters; it cannot be answered "
                              "here", hint="leave it: a request answers nothing until you grant it")
    typer.echo(f"{verb} {r['id']} for epic {r['epic']} (ticket {r['ticket']}, asked by {r['actor']} via {r['source']}):")
    if r["reason"]:
        typer.echo(f"  reason: {visible(r['reason'])}")
    typer.echo(f"  command | {visible(r['command'])}")
    typer.echo(f"  {r['sha'][:15]}…")
    return r


@permit_app.command("grant")
def grant(rid: str,
          for_epic: Annotated[bool, typer.Option(
              "--for-epic", help="Answer every later prompt for this exact command in this epic too (until the epic "
                                 "is done, paused or changed, or you revoke it). Default: once.")] = False,
          json_out: JsonOpt = False) -> None:
    """Grant a request for its exact command, once (default) or for the epic. Human only."""
    from orch.actor import confirm_typed, require_human_terminal
    from orch.core import permits
    cli, ws = _ctx()
    require_human_terminal("granting a permission")
    r = _show_request(ws, rid, "Granting" + (" for the whole epic" if for_epic else " once"))
    actor = confirm_typed(r["id"])
    entry = permits.permit_grant(ws, actor, r["id"], "epic" if for_epic else "once", expected_sha=r["sha"])
    cli._out(entry, json_out, f"{r['id']}: granted ({entry['scope']}), grant {entry['grant']}")


@permit_app.command("deny")
def deny(rid: str, json_out: JsonOpt = False) -> None:
    """Answer a request with no. Human only."""
    from orch.actor import confirm_typed, require_human_terminal
    from orch.core import permits
    cli, ws = _ctx()
    require_human_terminal("denying a permission")
    r = _show_request(ws, rid, "Denying")
    actor = confirm_typed(r["id"])
    entry = permits.permit_deny(ws, actor, r["id"], expected_sha=r["sha"])
    cli._out(entry, json_out, f"{r['id']}: denied")


@permit_app.command("revoke")
def revoke(grant_id: str, json_out: JsonOpt = False) -> None:
    """End a grant now; an action already running finishes. Human only."""
    from orch.actor import confirm_typed, require_human_terminal
    from orch.core import permits
    cli, ws = _ctx()
    require_human_terminal("revoking a permission")
    actor = confirm_typed(grant_id)
    entry = permits.permit_revoke(ws, actor, grant_id)
    cli._out(entry, json_out, f"grant {grant_id}: revoked")


@permit_app.command("hook")
def hook() -> None:
    """Claude Code PermissionRequest hook: answers from live signed grants in a factory epic; silent elsewhere."""
    import os
    from orch.core import permits
    from orch.core.workspace import Workspace
    try:
        payload = json.loads(sys.stdin.read() or "{}")
        if not isinstance(payload, dict):
            return
        ws = Workspace.open(Path(payload.get("cwd") or os.environ.get("CLAUDE_PROJECT_DIR") or Path.cwd()))
    except Exception:
        return  # not an orch workspace, or unreadable: no opinion, the harness asks as usual (never an allow)
    decision = permits.hook_decision(ws, payload)
    if decision is not None:
        typer.echo(json.dumps(decision))

