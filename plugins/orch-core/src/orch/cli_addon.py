"""`orch addon …` (spec A1 §6). Imports stay inside the commands so `import orch.cli` stays light."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated

import typer

addon_app = typer.Typer(no_args_is_help=True, help="Addons. list, check and preview are for everyone; the rest is human-only.")
JsonOpt = Annotated[bool, typer.Option("--json", help="Machine-readable output.")]


def _echo(data, as_json: bool, text: str) -> None:
    typer.echo(json.dumps(data, ensure_ascii=False, indent=2, default=str) if as_json else text)


def _current_workspace():
    from orch.core.workspace import Workspace
    from orch.errors import OrchError
    try:
        return Workspace.open()
    except OrchError:
        return None


@addon_app.command("list")
def list_(json_out: JsonOpt = False) -> None:
    """Default and custom addons with version, trust state and whether they are enabled in this workspace."""
    from orch.addons.discovery import discover
    from orch.addons.userfiles import TRUST_LABELS, trust_state, workspace_addons
    ws = _current_workspace()
    enabled = workspace_addons(ws.root) if ws else {}
    rows = []
    for f in discover():
        rows.append({"name": f.name, "kind": f.kind, "version": f.manifest.version if f.manifest else None,
                     "state": trust_state(f), "enabled": enabled.get(f.name, {}).get("enabled", False) if ws else None,
                     "error": f.error})
    lines = [f"{r['name']:<22} {r['kind']:<8} {r['version'] or '-':<9} {TRUST_LABELS[r['state']][1]:<18}"
             f"{' enabled here' if r['enabled'] else ''}{'  ' + r['error'] if r['error'] else ''}" for r in rows]
    _echo(rows, json_out, "\n".join(lines) or "no addons found")


@addon_app.command("check")
def check(
    path: Annotated[Path, typer.Argument(help="Addon folder (holds orch-addon.json), or the name of a known addon.")],
    static: Annotated[bool, typer.Option("--static", help="Only the static checks; do not import the addon's code.")] = False,
    strict: Annotated[bool, typer.Option("--strict", help="Design warnings (W1-W18, see DESIGN.md) fail the check too.")] = False,
    fmt: Annotated[str, typer.Option("--format", help="JSON shape: v1 (a flat problem list) or v2 "
                                                      "({\"errors\": [...], \"warnings\": [...]}).")] = "v1",
    json_out: JsonOpt = False,
) -> None:
    """Run the done-checklist from ADDONS.md: manifest, layout, imports and (unless --static) the contract suite,
    plus the design warnings from DESIGN.md (printed as △; they fail only with --strict)."""
    from orch.addons.check import static_problems
    from orch.addons.manifest import load_manifest
    from orch.errors import UsageError, ValidationError
    if fmt not in ("v1", "v2"):
        raise UsageError(f"--format {fmt!r} is not known", hint="use v1 or v2")
    if not path.is_dir() and len(path.parts) == 1:  # a bare name, as list/trust/enable take: use that addon's folder
        from orch.addons.discovery import find
        found = find(str(path))
        if found is not None:
            path = found.folder
    if not path.is_dir():
        missing = f"{path} is not a folder"
        if json_out:  # ruling F4: one JSON document and exit 5, also for a missing path
            _echo({"errors": [missing], "warnings": []} if fmt == "v2" else [missing], True, "")
            raise typer.Exit(ValidationError.exit_code)
        raise ValidationError(missing, hint="pass the folder that holds orch-addon.json")
    problems = static_problems(path)
    warnings: list[str] = []
    if not problems and not static:
        from orch.testing import run_addon_check
        problems, warnings = run_addon_check(path)
    failed = bool(problems) or (strict and bool(warnings))
    if json_out:  # ruling F4: exactly one JSON document, exit 5 like a ValidationError on failure
        _echo({"errors": problems, "warnings": warnings} if fmt == "v2" else (problems or ([] if not strict else warnings)),
              True, "")
        if failed:
            raise typer.Exit(ValidationError.exit_code)
        return
    lines = [f"✗ {p}" for p in problems] + [f"△ {w}" for w in warnings]
    if lines:
        typer.echo("\n".join(lines))
    if problems:
        raise ValidationError(f"{len(problems)} problem(s) in {path}")
    if failed:
        raise ValidationError(f"{len(warnings)} design warning(s) in {path} (--strict)",
                              hint="see DESIGN.md for each W rule; without --strict they do not fail the check")
    m = load_manifest(path)
    note = f" with {len(warnings)} design warning{'s' if len(warnings) != 1 else ''} (△, see DESIGN.md)" if warnings else ""
    typer.echo(f"✓ {m.name} {m.version} passes orch addon check" + (" (static only)" if static else "") + note)


@addon_app.command("preview")
def preview(
    target: Annotated[str, typer.Argument(help="Addon folder (holds orch-addon.json), or the name of a known addon.")],
    slot: Annotated[str, typer.Option("--slot", help="page.<name>, ticket.code|sync|external|pages, today.summary, "
                                                     "today.from_addons (its decisions) or board.external.")],
    out: Annotated[Path, typer.Option("--out", help="The file to write: .html, or .png (needs Playwright).")],
    ticket: Annotated[str | None, typer.Option("--ticket", help="The ticket a ticket.* slot is drawn for.")] = None,
    param: Annotated[list[str] | None, typer.Option("--param", help="A page's query parameter, key=value; repeat.")] = None,
    width: Annotated[int | None, typer.Option("--width", help="Frame width in px (default 1280; 360 for ticket.*).")] = None,
    theme: Annotated[str, typer.Option("--theme", help="light or dark.")] = "light",
    fetch: Annotated[bool, typer.Option("--fetch", help="Run the addon's providers once first (its own ctx.run).")] = False,
    fixtures: Annotated[Path | None, typer.Option("--fixtures", help="Recorded command output to fetch from instead "
                                                                     "(the FakeRunner format of orch.testing).")] = None,
) -> None:
    """Draw one addon slot as Mission Control does, into an HTML file or a PNG, without a server or the dashboard
    token. Read-only: nothing on the page does anything, and fetched data goes to a temporary folder."""
    from orch.errors import UsageError
    try:
        from orch.dashboard import preview as pv
    except ImportError:
        raise UsageError("orch addon preview needs the dashboard extra", hint="install orch-core[dashboard]") from None
    ws = _current_workspace()
    if ws is None:
        raise UsageError("orch addon preview runs inside an orch workspace", hint="cd into one, or run orch init")
    params = {}
    for p in param or []:
        key, eq, value = p.partition("=")
        if not eq or not key:
            raise UsageError(f"--param {p!r} is not key=value")
        params[key] = value
    if out.suffix not in (".html", ".png"):
        raise UsageError(f"--out {out} must end in .html or .png")
    if fixtures is not None and not fixtures.is_dir():
        raise UsageError(f"--fixtures {fixtures} is not a folder")
    html = pv.render(ws, target, slot, ticket=ticket, params=params, width=width, theme=theme, fetch=fetch,
                     fixtures=fixtures)
    out.parent.mkdir(parents=True, exist_ok=True)
    if out.suffix == ".png":
        import tempfile
        with tempfile.TemporaryDirectory(prefix="orch-preview-") as tmp:
            page = Path(tmp) / "preview.html"
            page.write_text(html, encoding="utf-8")
            pv.screenshot(page, out, width or (360 if slot.startswith("ticket.") else 1280), theme)
    else:
        out.write_text(html, encoding="utf-8")
    typer.echo(f"wrote {out}")


def _human(what: str) -> None:
    from orch.actor import require_human_terminal
    require_human_terminal(what, hint="addons are installed, trusted and enabled by the human: run this in your own "
                                      "terminal or use Mission Control → Workspace & addons")


def _confirm(name: str, verb: str) -> None:
    from orch.errors import HumanOnlyError
    if input(f"Type {name} to {verb} it: ").strip() != name:
        raise HumanOnlyError("confirmation did not match; nothing changed")


def _workspace_root():
    from orch.core.workspace import Workspace
    return Workspace.open().root


@addon_app.command("install")
def install(source: Annotated[str, typer.Argument(help="Addon folder or git URL.")],
            ref: Annotated[str | None, typer.Option("--ref", help="Branch or tag (git only).")] = None,
            path: Annotated[str | None, typer.Option("--path", help="The addon's folder inside the repository (git only), e.g. addons/my-addon.")] = None) -> None:
    """Copy (folder) or clone (git) an addon into ~/.config/orch/addons/; it stays untrusted until you trust it."""
    _human("installing an addon")
    from orch.addons import manage
    m = manage.install(source, ref=ref, subpath=path)
    typer.echo(f"installed {m.name} {m.version} (not trusted yet): review and trust it with `orch addon trust {m.name}`")


@addon_app.command("update")
def update(name: Annotated[str | None, typer.Argument()] = None,
           all_: Annotated[bool, typer.Option("--all", help="Every installed custom addon.")] = False,
           check_only: Annotated[bool, typer.Option("--check", help="Only say whether an update is available.")] = False) -> None:
    """Check for or apply updates; an applied update stays disabled until you trust it again."""
    _human("updating addons")
    from orch.addons import manage
    from orch.errors import UsageError
    if (name is None) == (not all_):
        raise UsageError("pass an addon name or --all")
    from orch.errors import OrchError, ValidationError
    infos = manage.update_check(None if all_ else name)
    failed = []
    for info in infos:
        if check_only or not info.has_update:
            typer.echo(f"{info.name}: {info.message}")
            if info.failed:
                failed.append(info.name)
            continue
        try:
            m = manage.update_apply(info.name)
        except OrchError as e:  # --all: report it and go on with the others
            if not all_:
                raise
            failed.append(info.name)
            typer.echo(f"{info.name}: update failed: {e.message}")
            continue
        typer.echo(f"updated {info.name} {info.current} → {m.version}; it is disabled until you run `orch addon trust {info.name}`")
    if failed:
        raise ValidationError(f"{len(failed)} update(s) failed or could not be checked: {', '.join(failed)}")


def review_text(r) -> str:
    """What the human sees before being asked: new permissions, changed files, the changelog."""
    lines = [f"{r.name} {r.version} from {r.source}" + (f" (trusted before: {r.old_version})" if r.old_version else "")]
    label = {"capabilities": "capability", "binaries": "binary", "env": "env", "actions": "action",
             "uploads": "file upload action", "remote_actions": "action a paired device may run"}
    news = [f"  + {label[kind]} {v}" for kind, values in r.added.items() for v in values]
    if r.remote_humans_added:
        news.append("  ! remote_humans: paired phones may answer and decide for you")
    lines.append("New permissions:" if news else "New permissions: none")
    lines.extend(news)
    if r.api_change:
        lines.append(f"  ! requires_api {r.api_change[0]} → {r.api_change[1]}")
    lines.append(f"Changed files ({len(r.changed)}):")
    lines.extend(f"  {c}" for c in r.changed[:50])
    if len(r.changed) > 50:
        lines.append(f"  … and {len(r.changed) - 50} more")
    if r.changelog:
        lines.append("Changelog:")
        lines.extend(f"  {line}" for line in r.changelog.splitlines()[:20])
    lines.append("Trusting runs this code inside orch serve with your permissions. Only trust code you have read.")
    return "\n".join(lines)


@addon_app.command("trust")
def trust(name: str) -> None:
    """Show what changed and which permissions are new, then trust this exact version (runs the contract suite)."""
    _human("trusting an addon")
    from orch.addons import manage
    r = manage.review(name)
    typer.echo(review_text(r))
    _confirm(name, "trust")
    digest = manage.trust_addon(name, seen_digest=r.digest)
    typer.echo(f"trusted {name} {r.version} ({digest[:12]}); enable it per workspace with `orch addon enable {name}`")


@addon_app.command("enable")
def enable(name: str) -> None:
    """Enable a trusted addon for the current workspace (per user)."""
    _human("enabling an addon")
    from orch.addons import manage
    root = _workspace_root()
    manage.enable(root, name)
    typer.echo(f"enabled {name} for {root}; restart orch serve if it is running")


@addon_app.command("disable")
def disable(name: str) -> None:
    """Disable an addon for the current workspace."""
    _human("disabling an addon")
    from orch.addons import manage
    root = _workspace_root()
    manage.disable(root, name)
    typer.echo(f"disabled {name} for {root}")


@addon_app.command("rollback")
def rollback(name: str) -> None:
    """Go back to the version before the last update (one level)."""
    _human("rolling back an addon")
    from orch.addons import manage
    m = manage.rollback(name)
    typer.echo(f"rolled {name} back to {m.version}")


@addon_app.command("remove")
def remove(name: str) -> None:
    """Delete a custom addon, its previous version, its trust and its enable entries."""
    _human("removing an addon")
    from orch.addons import manage
    _confirm(name, "remove")
    manage.remove(name)
    typer.echo(f"removed {name}")


@addon_app.command("setup")
def setup(name: Annotated[str, typer.Argument(help="The addon; only ticket-usage has a setup step.")]) -> None:
    """Install what an addon needs outside orch. ticket-usage: copy the limits recorder to your Claude dir and add it
    as your status line in the user-global Claude settings (an existing status line is never replaced)."""
    from orch.actor import require_human_terminal
    from orch.addons import usage_recorder as rec
    from orch.errors import HumanOnlyError, UsageError
    if name != rec.ADDON:
        raise UsageError(f"{name} has no setup step", hint=f"only {rec.ADDON} has one: {rec.SETUP_CMD}")
    # It writes a script every Claude Code session runs and the user-global settings: the human's call.
    require_human_terminal("setting up the ticket-usage recorder",
                           hint=f"it changes your user-global Claude settings: run `{rec.SETUP_CMD}` in your own terminal")
    p = rec.plan()
    typer.echo(rec.plan_text(p))
    if p.copy or p.add_status_line:
        if input(f"Type {name} to go ahead: ").strip() != name:
            raise HumanOnlyError("confirmation did not match; nothing changed")
        for line in rec.apply(p):
            typer.echo(line)
    manual = rec.manual_steps(p)
    if manual:
        typer.echo(manual)
    elif p.copy or p.add_status_line:
        typer.echo("Limits appear on the Usage page after Claude Code's next reply.")
    else:
        typer.echo("Nothing to change: the recorder is set up.")


option_app = typer.Typer(no_args_is_help=True, help="Yes/no options enabled addons add to a ticket. list is for everyone "
                                                    "(agents may read them); set is human-only.")
addon_app.add_typer(option_app, name="ticket-option")


@option_app.command("list")
def option_list(ref: Annotated[str, typer.Argument(help="A ticket (ID, number or external key).")],
                json_out: JsonOpt = False) -> None:
    """The options of enabled, trusted addons on a ticket, with their values."""
    from orch.addons import ticket_options
    from orch.core import store
    ws = _current_workspace()
    if ws is None:
        raise typer.Exit(2)
    tid = store.resolve(ws, ref).id
    rows = [{"option": o.key, "label": o.label, "value": o.value} for o in ticket_options.views(ws, tid)]
    _echo({"ticket": tid, "options": rows}, json_out,
          "\n".join(f"{r['option']:<28} {'on' if r['value'] else 'off'}  {r['label']}" for r in rows) or "no ticket options")


@option_app.command("set")
def option_set(ref: Annotated[str, typer.Argument(help="A ticket (ID, number or external key).")],
               option: Annotated[str, typer.Argument(help="<addon>/<option>, as `list` shows it.")],
               state: Annotated[str, typer.Argument(help="on | off")]) -> None:
    """Turn an addon's ticket option on or off. Human-only: an agent (or a call without a terminal) is refused,
    because the option is a human decision (for example whether a ticket may notify your phone)."""
    from orch.actor import confirm_typed, require_human_terminal
    from orch.addons import ticket_options
    from orch.core import store
    from orch.errors import UsageError
    if state not in ("on", "off"):
        raise UsageError("state is on or off")
    require_human_terminal("setting a ticket option")
    ws = _current_workspace()
    if ws is None:
        raise typer.Exit(2)
    tid = store.resolve(ws, ref).id
    actor = confirm_typed(tid)
    addon, _, option_id = option.partition("/")
    changed = ticket_options.set_value(ws, tid, addon, option_id, state == "on", actor)
    typer.echo(f"{tid}: {option} {state}" + ("" if changed else " (already)"))
