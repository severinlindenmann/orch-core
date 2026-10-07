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
    if r.get("allowed"):
        cli._out(r, json_out, "already allowed by the Dark profile: just run it (nothing was filed)")
        return
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


@permit_app.command("show")
def show(rid: str, json_out: JsonOpt = False) -> None:
    """What came of one permission request: open (no answer yet), granted (run the command again now) or denied. For
    agents too: a plain read-only command, so a session can look instead of waiting."""
    from orch.core import permits
    from orch.errors import NotFoundError
    cli, ws = _ctx()
    r = permits.requests(ws).get(rid.upper())
    if r is None:
        raise NotFoundError(f"no permission request {rid}")
    e = permits.decisions(ws).get((r["id"], r["sha"]))
    if e is not None:
        state = "granted" if e.get("kind") == "grant" else "denied"
    else:
        state = "open" if any(o["id"] == r["id"] for o in permits.open_requests(ws)) else "allowed by the Dark profile"
    say = {"granted": "granted: run the command again now, exactly as before",
           "denied": "denied: do the work without it, or end your turn with orch log saying what is missing",
           "open": "open: no answer yet; do not wait for it, go on with other steps or end your turn",
           "allowed by the Dark profile": "allowed by the Dark profile now: run the command again"}[state]
    cli._out({"id": r["id"], "ticket": r["ticket"], "state": state, "command": r["command"]}, json_out,
             f"{r['id']} ({r['ticket']}): {say}")


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


@factory_app.command("on")
def factory_on(json_out: JsonOpt = False) -> None:
    """Switch AI Factory on in this checkout: signed into the approval ledger and written to factory.enabled. Human
    only (your own terminal, typed confirmation): the config value alone switches nothing on."""
    from orch.actor import confirm_typed, require_human_terminal
    cli, ws = _ctx()
    require_human_terminal("turning on AI Factory")
    typer.echo("AI Factory: epics you start as a factory are split, approved and built by agents within the "
               "charter you sign.", err=json_out)
    cli._ops(ws, confirm_typed("FACTORY")).set_factory(True)
    _factory_said(cli, ws, json_out)


@factory_app.command("off")
def factory_off(json_out: JsonOpt = False) -> None:
    """Switch AI Factory off in this checkout. Anyone may: it only takes power away."""
    cli, ws = _ctx()
    cli._ops(ws).set_factory(False)
    _factory_said(cli, ws, json_out)


@factory_app.command("status")
def factory_status(json_out: JsonOpt = False) -> None:
    """Whether AI Factory is on in this checkout (signed), and why not."""
    cli, ws = _ctx()
    _factory_said(cli, ws, json_out)


def _factory_said(cli, ws, json_out) -> None:
    from orch.core import permits
    on = permits.enabled(ws)
    cli._out({"factory": on, "config": permits.config_enabled(ws), "dark": permits.dark_on(ws)}, json_out,
             "AI Factory is on (signed)" if on else permits.off_reason(ws))


@factory_app.command("dark")
def factory_dark(state: Annotated[str, typer.Argument(help="on | off | status")] = "status",
                 json_out: JsonOpt = False) -> None:
    """Dark AI Factory in this checkout. `on` is the human's decision, signed into the approval ledger (run it in
    your own terminal); `off` anyone may run. It counts only while AI Factory is on (`orch factory on`)."""
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
             + ("" if permits.enabled(ws) else f" ({permits.off_reason(ws)})"))


release_app = typer.Typer(no_args_is_help=True, help="Dark AI Factory's release recipe on this machine (human only).")
factory_app.add_typer(release_app, name="release")


def _recipe_text(rec: dict) -> str:
    return json.dumps(rec, indent=1, ensure_ascii=True)  # validated printable ASCII; escaped as JSON besides


@release_app.command("set")
def release_set(file: Annotated[Path, typer.Option("--file", help="The recipe, a JSON file of yours.")],
                json_out: JsonOpt = False) -> None:
    """Store this workspace's release recipe in your orch config dir. Human only: prints the whole recipe and needs
    RELEASE typed. Never read from the workspace config or anything an agent writes."""
    from orch.actor import confirm_typed, require_human_terminal
    from orch.core import factory_release
    from orch.errors import UsageError
    cli, ws = _ctx()
    require_human_terminal("setting the release recipe")
    try:
        data = json.loads(file.read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeDecodeError) as e:
        raise UsageError(f"{file} is not a readable JSON file ({type(e).__name__})") from None
    rec = factory_release.check_recipe(data, ws)  # refused before anything is shown or asked
    pins = factory_release.pin_programs(rec)
    typer.echo("Setting the release recipe of this workspace (Dark epics that sign a release run these commands "
               "by themselves once Ready, in the runner's own repository, up to the stage each charter signs; "
               "production only for a charter that signs prod, never before its release window opens):",
               err=json_out)
    typer.echo(_recipe_text(rec), err=json_out)
    typer.echo("Programs, pinned by real path and sha256 (a release refuses to run one that changed):", err=json_out)
    for word, pin in pins.items():
        typer.echo(f"  {word} -> {pin['path']}  sha256 {pin['sha256']}", err=json_out)
    from orch.core.epics import FACTORY_DEFAULTS
    prod = next((s for s in rec["stages"] if s["name"] == "production"), None)
    if prod and prod["window_hours"] >= FACTORY_DEFAULTS["max_hours"]:
        typer.echo(f"Warning: the production window ({prod['window_hours']} hours) is as long as or longer than a "
                   f"factory charter's default time budget ({FACTORY_DEFAULTS['max_hours']} hours): such a charter "
                   "cannot sign prod (waiting for the window would use the whole budget).", err=True)
    for eid, hours in (factory_release.prod_charters(ws) if prod else []):
        if prod["window_hours"] >= hours:
            typer.echo(f"Warning: {eid}'s live charter signs prod with a {hours}-hour budget: with this "
                       f"{prod['window_hours']}-hour window its production may never run under that charter.",
                       err=True)
    rec = factory_release.set_recipe(ws, confirm_typed("RELEASE"), data, shown=pins)
    cli._out(rec, json_out, f"release recipe set: {', '.join(s['name'] for s in rec['stages'])}")


@release_app.command("show")
def release_show(json_out: JsonOpt = False) -> None:
    """This workspace's release recipe, or why there is none. Human only."""
    from orch.actor import require_human_terminal
    from orch.core import factory_release
    cli, ws = _ctx()
    require_human_terminal("showing the release recipe")
    rec, why = factory_release.load(ws)
    cli._out({"recipe": rec, "why": why}, json_out, _recipe_text(rec) if rec else why)


@release_app.command("clear")
def release_clear(json_out: JsonOpt = False) -> None:
    """Remove this workspace's release recipe: no Dark epic releases until you set one again. Human only."""
    from orch.actor import confirm_typed, require_human_terminal
    from orch.core import factory_release
    cli, ws = _ctx()
    require_human_terminal("clearing the release recipe")
    typer.echo("Clearing the release recipe of this workspace: no release runs until you set one again.", err=json_out)
    had = factory_release.clear_recipe(ws, confirm_typed("CLEAR"))
    cli._out({"cleared": had}, json_out, "release recipe cleared" if had else "there was no release recipe")


@release_app.command("resolve")
def release_resolve(epic: str, reason: Annotated[str, typer.Option("--reason", help="Why the hold can be lifted.")],
                    json_out: JsonOpt = False) -> None:
    """Lift the hold an epic's unresolved production puts on every other epic, without running it again. Human only:
    needs the epic id typed."""
    from orch.actor import confirm_typed, require_human_terminal
    from orch.core import factory_release
    cli, ws = _ctx()
    require_human_terminal("resolving a production hold")
    eid = str(epic).upper()
    typer.echo(f"Resolving the production hold of {eid}: other epics' production may run again; {eid}'s own "
               "production does not run unless you retry it.", err=json_out)
    text = factory_release.resolve(ws, confirm_typed(eid), eid, reason)
    cli._out({"epic": eid, "resolved": True}, json_out, text)


@release_app.command("clear-window")
def release_clear_window(json_out: JsonOpt = False) -> None:
    """Stop production times recorded beyond now (a future-dated record) from keeping the release window shut.
    Human only: needs WINDOW typed. Earlier times still count; nothing is deleted."""
    from orch.actor import confirm_typed, require_human_terminal
    from orch.core import factory_release
    cli, ws = _ctx()
    require_human_terminal("clearing a future-dated release window")
    typer.echo("Production times recorded beyond now will no longer keep the release window shut.", err=json_out)
    cli._out({"cleared": True}, json_out, factory_release.clear_window(ws, confirm_typed("WINDOW")))


@release_app.command("retry")
def release_retry(epic: str,
                  stage: Annotated[str, typer.Option("--stage", help="merge, dev or production")],
                  child: Annotated[Optional[str], typer.Option(
                      "--child", help="The child, for a stage that runs per child (default: the epic).")] = None,
                  json_out: JsonOpt = False) -> None:
    """Allow one more attempt at a failed (or unknown) release stage. Human only; the runner runs it."""
    from orch.actor import confirm_typed, require_human_terminal
    from orch.core import factory_release, store
    cli, ws = _ctx()
    require_human_terminal("retrying a release stage")
    eid = store.resolve(ws, epic).id
    unit = store.resolve(ws, child).id if child else eid
    typer.echo(f"Retrying the {stage} stage of {unit} in epic {eid}: the runner runs it once more.", err=json_out)
    text = factory_release.retry(ws, confirm_typed(eid), eid, stage, unit)
    cli._out({"epic": eid, "stage": stage, "unit": unit}, json_out, text)


clones_app = typer.Typer(no_args_is_help=True, help="The runner's per-child clones on this machine (human only).")
factory_app.add_typer(clones_app, name="clones")


@clones_app.command("list")
def clones_list(json_out: JsonOpt = False) -> None:
    """The clones the runner made for this workspace's children. Human only; the runner never deletes one."""
    from orch.actor import require_human_terminal
    from orch.core import factory_clones
    cli, ws = _ctx()
    require_human_terminal("listing the child clones")
    rows = factory_clones.listing(ws)
    cli._out({"clones": rows}, json_out, "\n".join(f"{r['child']}  {r['branch']}  {r['path']}" for r in rows)
             or "no child clones")


@clones_app.command("trust")
def clones_trust(json_out: JsonOpt = False) -> None:
    """Which clone folders Claude Code still asks its folder-trust question for, and what to add. Human only; orch
    never writes Claude's settings: open Claude once in each folder and accept, or add the printed entries yourself
    (under `projects` in Claude's .claude.json, with Claude closed)."""
    import json
    from orch.actor import require_human_terminal
    from orch.core import factory_runner
    cli, ws = _ctx()
    require_human_terminal("reading the clones' trust")
    rows = factory_runner.clone_trust(ws)
    left = [r for r in rows if not r["trusted"]]
    entries = {r["path"]: {"hasTrustDialogAccepted": True} for r in left}
    text = ("no child clones" if not rows else "every clone folder is trusted" if not left else
            "Claude Code asks its folder-trust question for these clone folders (trust of a folder above a clone is "
            "not used for it):\n" + "\n".join(f"  {r['child']}  {r['path']}" for r in left)
            + "\nOpen Claude once in each folder and accept, or add these entries under \"projects\" in your "
              ".claude.json (with Claude closed):\n" + json.dumps(entries, indent=2))
    cli._out({"clones": rows, "add": entries}, json_out, text)


@clones_app.command("clean")
def clones_clean(child: str, json_out: JsonOpt = False) -> None:
    """Delete one child's clone and the runner's record of it, work that was not released included. Human only:
    needs the child id typed; refused while a session runs in it."""
    from orch.actor import confirm_typed, require_human_terminal
    from orch.core import factory_clones, factory_sessions
    from orch.errors import UsageError
    cli, ws = _ctx()
    require_human_terminal("removing a child's clone")
    cid = child.upper()
    rec = factory_clones.record(ws, cid)
    if rec is None:
        raise UsageError(f"the runner has no clone of {cid}")
    if any(b["child"] == cid for b in factory_sessions.bindings(ws)):
        raise UsageError(f"a session of {cid} runs in its clone: stop the run first")
    typer.echo(f"Deleting the clone of {cid} at {rec['path']} (branch {rec['branch']}), with any work in it that was "
               "not released:", err=json_out)
    had = factory_clones.clean(ws, confirm_typed(cid), cid)
    cli._out({"child": cid, "removed": had}, json_out, f"the clone of {cid} was removed" if had else "nothing removed")


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
                baseline: Annotated[bool, typer.Option(
                    "--baseline", help="A named set of rules: `orch` (default; the orch agent verbs a planner or "
                                       "worker session needs) or `git-basic` (git status, diff, log, show, add, "
                                       "commit, branch --show-current).")] = False,
                name: Annotated[Optional[str], typer.Argument(
                    help="With --baseline: which one (orch or git-basic).", show_default=False)] = None,
                json_out: JsonOpt = False) -> None:
    """Add a rule to the Dark profile. Human only."""
    from orch.actor import confirm_typed, require_human_terminal
    from orch.core import dark_profile
    from orch.errors import UsageError
    cli, ws = _ctx()
    if sum(x is not None for x in (prefix, exact, from_request)) + baseline != 1:
        raise UsageError("give exactly one of --prefix, --exact, --from-request or --baseline")
    if name is not None and not baseline:
        raise UsageError("a name goes with --baseline only")
    require_human_terminal("changing the Dark profile")
    if baseline:
        which = name or "orch"
        todo = dark_profile.baseline_todo(ws, which)  # an unknown name lists the baselines
        if not todo:
            cli._out({"added": [], "failed": []}, json_out, f"every rule of the {which} baseline is in the Dark "
                                                            "profile already")
            return
        typer.echo(f"Adding the {which} baseline to the Dark profile (every later Dark run in this workspace may run "
                   "them without asking):", err=json_out)
        for r in todo:
            kind = dark_profile.baseline_kind(which, r)
            value = r.split() if kind == "prefix" else r
            typer.echo("  " + _rule_line({"id": dark_profile.rule_id(kind, value), "kind": kind, "rule": value}),
                       err=json_out)
        res = dark_profile.add_baseline(ws, confirm_typed("BASELINE"), shown=todo, name=which)
        lines = [f"added {len(res['added'])} baseline rules to the Dark profile"]
        lines += [f"  added  {e['rule_id']}  {dark_profile.text(e['rule_kind'], e['rule'])}" for e in res["added"]]
        lines += [f"  FAILED {f['rule']}: {f['error']}" for f in res["failed"]]
        cli._out(res, json_out, "\n".join(lines))
        if res["failed"]:
            from orch.errors import ValidationError
            raise ValidationError(f"{len(res['failed'])} baseline rules were not added (listed above); the others were",
                                  hint=f"run orch dark profile add --baseline {which} again after fixing the cause")
        return
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


@profile_app.command("prune")
def profile_prune(json_out: JsonOpt = False) -> None:
    """Remove exact rules for commands that are not one plain command (chains, pipes, redirects, substitutions):
    they match only that identical text again. Human only: lists them and needs PRUNE typed."""
    from orch.actor import confirm_typed, require_human_terminal
    from orch.core import dark_profile
    cli, ws = _ctx()
    require_human_terminal("changing the Dark profile")
    todo = dark_profile.prunable(ws)
    if not todo:
        cli._out({"removed": []}, json_out, "nothing to prune: no exact rule for a compound command")
        return
    typer.echo("Removing from the Dark profile (exact rules for compound commands, which match only that identical "
               "text again):", err=json_out)
    for r in todo:
        typer.echo("  " + _rule_line(r), err=json_out)
    removed = dark_profile.prune(ws, confirm_typed("PRUNE"), [r["id"] for r in todo])
    cli._out({"removed": removed}, json_out, f"removed {len(removed)} rules from the Dark profile")


@permit_app.command("hook")
def hook() -> None:
    """Claude Code PermissionRequest hook: answers from live signed grants in a factory epic; silent elsewhere."""
    import os
    from orch.core import permits
    from orch.core.workspace import Workspace
    from orch.core import factory_sessions
    payload = None
    try:
        payload = json.loads(sys.stdin.read() or "{}")
        if not isinstance(payload, dict):
            return
        start = Path(payload.get("cwd") or os.environ.get("CLAUDE_PROJECT_DIR") or Path.cwd())
        ws = Workspace.open(start) if permits.enabled_at(start) else None  # fast path: off, read without opening
    except Exception:
        ws = None
    if ws is None:
        # The factory is off or the workspace unreadable: no opinion (the harness asks as usual), except for a session
        # the runner may have bound, which is denied: an agent that broke the config must not get the harness's prompt
        # in place of the factory's rules.
        if isinstance(payload, dict) and factory_sessions.recorded(payload.get("session_id")):
            typer.echo(json.dumps(permits.deny_unreadable()))
        return
    decision = permits.hook_decision(ws, payload)
    if decision is not None:
        typer.echo(json.dumps(decision))

