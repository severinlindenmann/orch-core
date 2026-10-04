"""`orch permit …` (AI Factory, #2): permission requests in a factory epic and the human's signed answers.
Agents may `request` and `list`; `grant`, `deny` and `revoke` are the human's; `hook` is Claude Code's
PermissionRequest hook (orch.core.permits). `orch dark profile …` (Dark AI Factory): anyone may `list`; `add` and
`remove` are the human's (orch.core.dark_profile)."""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Annotated, Optional

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
    cli, ws = _ctx()
    reqs, grants, cards = permits.open_requests(ws), permits.grants(ws), permits.budget_cards(ws)
    lines = [f"needs you: {c['epic']} {permits.shown(c['title'])}: {c['reason']}" for c in cards]
    lines += [f"{r['id']:<10} {r['epic']} {r['ticket']} asks: {permits.shown(r['command'])}" for r in reqs] \
        or ["no open requests"]
    lines += [f"grant {g['grant']} ({g['scope']}) {g['epic']}: {permits.shown(g['command'])}"
              + ("" if g["live"] else f" · {g['why']}") for g in grants]
    cli._out({"requests": reqs, "grants": grants, "cards": cards}, json_out, "\n".join(lines))


def _show_request(ws, rid: str, verb: str) -> dict:
    """Print what the answer binds: every character outside printable ASCII escaped, so nothing hides."""
    from orch.core import permits
    from orch.errors import NotFoundError
    r = permits.requests(ws).get(rid.upper())
    if r is None:
        raise NotFoundError(f"no permission request {rid}")
    typer.echo(f"{verb} {r['id']} for epic {r['epic']} (ticket {r['ticket']}, asked by {permits.shown(r['actor'])} "
               f"via {permits.shown(r['source'])}):")
    if r["reason"]:
        typer.echo(f"  reason: {permits.shown(r['reason'])}")
    typer.echo(f"  command | {permits.shown(r['command'])}")
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


factory_app = typer.Typer(no_args_is_help=True, help="AI Factory switches that are signed, not config values.")


@factory_app.command("dark")
def factory_dark(state: Annotated[str, typer.Argument(help="on | off | status")] = "status",
                 json_out: JsonOpt = False) -> None:
    """Dark AI Factory in this checkout. `on` is the human's decision, signed into the approval ledger (run it in
    your own terminal); `off` anyone may run. It counts only while factory.enabled is on."""
    from orch.core import permits
    from orch.errors import UsageError
    cli, ws = _ctx()
    if state not in ("on", "off", "status"):
        raise UsageError("expected on, off or status")
    if state == "on":
        from orch.actor import confirm_typed, require_human_terminal
        require_human_terminal("turning on Dark AI Factory")
        typer.echo("Dark AI Factory: epics you start with --dark run without asking you, from this checkout's Dark "
                   "profile.", err=json_out)
        cli._ops(ws, confirm_typed("DARK")).set_factory_dark(True)
    elif state == "off":
        cli._ops(ws).set_factory_dark(False)
    on = permits.dark_on(ws)
    cli._out({"dark": on, "factory": permits.enabled(ws)}, json_out,
             "Dark AI Factory is on (signed)" if on else "Dark AI Factory is off"
             + ("" if permits.enabled(ws) else " (factory.enabled is off)"))


dark_app = typer.Typer(no_args_is_help=True, help="Dark AI Factory: this checkout's Dark profile.")
profile_app = typer.Typer(no_args_is_help=True, help="The shell commands a Dark factory epic runs without asking you.")
dark_app.add_typer(profile_app, name="profile")


def _rule_line(r: dict) -> str:
    from orch.core import dark_profile, permits
    return f"{r['id']}  {r['kind']:<6} {permits.shown(dark_profile.text(r['kind'], r['rule']))}"


@profile_app.command("list")
def profile_list(json_out: JsonOpt = False) -> None:
    """The rules in force (anyone may read them)."""
    from orch.core import dark_profile, permits
    cli, ws = _ctx()
    rules = dark_profile.rules(ws)
    lines = [] if permits.dark_on(ws) else ["Dark AI Factory is switched off in this checkout: the profile answers "
                                            "nothing (`orch factory dark status`)"]
    lines += [_rule_line(r) for r in rules] or ["the Dark profile is empty"]
    cli._out({"rules": rules, "dark": permits.dark_on(ws)}, json_out, "\n".join(lines))


@profile_app.command("add")
def profile_add(prefix: Annotated[Optional[str], typer.Option(
                    "--prefix", help='Plain words a single simple command starts with, e.g. "npm run verify".')] = None,
                exact: Annotated[Optional[str], typer.Option("--exact", help="The full command text.")] = None,
                from_request: Annotated[Optional[str], typer.Option(
                    "--from-request", help="An open Dark request (P-…): its command as an exact rule.")] = None,
                json_out: JsonOpt = False) -> None:
    """Add a rule to the Dark profile. Human only."""
    from orch.actor import confirm_typed, require_human_terminal
    from orch.core import dark_profile
    from orch.errors import UsageError
    cli, ws = _ctx()
    if sum(x is not None for x in (prefix, exact, from_request)) != 1:
        raise UsageError("give exactly one of --prefix, --exact or --from-request")
    require_human_terminal("changing the Dark profile")
    if from_request is not None:
        r = _show_request(ws, from_request, "Adding to the Dark profile, as an exact rule, the command of")
        actor = confirm_typed(r["id"])
        entry = dark_profile.add_from_request(ws, actor, r["id"], expected_sha=r["sha"])
    else:
        kind, value = dark_profile.check_rule(ws, "prefix" if prefix is not None else "exact",
                                              prefix if prefix is not None else exact)
        rid = dark_profile.rule_id(kind, value)
        typer.echo("Adding to the Dark profile (every later Dark run in this workspace may run it without asking):")
        typer.echo("  " + _rule_line({"id": rid, "kind": kind, "rule": value}))
        actor = confirm_typed(rid)
        entry = dark_profile.add(ws, actor, kind, value)
    cli._out(entry, json_out, f"{entry['rule_id']}: added to the Dark profile")


@profile_app.command("remove")
def profile_remove(rule_id: str, json_out: JsonOpt = False) -> None:
    """Take a rule out of the Dark profile. Human only."""
    from orch.actor import confirm_typed, require_human_terminal
    from orch.core import dark_profile
    from orch.errors import NotFoundError
    cli, ws = _ctx()
    require_human_terminal("changing the Dark profile")
    r = next((x for x in dark_profile.rules(ws) if x["id"] == rule_id), None)
    if r is None:
        raise NotFoundError(f"no rule {rule_id} in the Dark profile")
    typer.echo("Removing from the Dark profile:\n  " + _rule_line(r))
    actor = confirm_typed(r["id"])
    entry = dark_profile.remove(ws, actor, r["id"])
    cli._out(entry, json_out, f"{r['id']}: removed from the Dark profile")


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
        start = Path(payload.get("cwd") or os.environ.get("CLAUDE_PROJECT_DIR") or Path.cwd())
        if not permits.enabled_at(start):
            return  # fast path: the factory is off (read without opening the workspace)
        ws = Workspace.open(start)
    except Exception:
        return  # not an orch workspace, or unreadable: no opinion, the harness asks as usual (never an allow)
    decision = permits.hook_decision(ws, payload)
    if decision is not None:
        typer.echo(json.dumps(decision))

