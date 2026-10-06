"""`orch quick …`: quick tasks, one-line jobs too small for a ticket (orch.core.quick)."""
from __future__ import annotations

from pathlib import Path
from typing import Annotated, Optional

import typer

quick_app = typer.Typer(help="Quick tasks: one-line jobs too small for a ticket. No requirements, plan or verdict; "
                             "done with one line of proof, and a ticket once it outgrows the size limit.")
quick_artifact_app = typer.Typer(no_args_is_help=True, help="Files and links that prove a quick task.")
quick_app.add_typer(quick_artifact_app, name="artifact")

JsonOpt = Annotated[bool, typer.Option("--json", help="Machine-readable output.")]
RefArg = Annotated[str, typer.Argument(metavar="QUICK", help="Quick task, e.g. Q-12 or 12.")]
NoteOpt = Annotated[Optional[str], typer.Option("--message", "-m", help="One line: what was done, or why.")]


def _ctx():
    from orch import cli
    return cli, cli._ws()


def _qops(ws, actor=None):
    from orch.actor import cli_actor
    from orch.core.quick import QuickOps
    return QuickOps(ws, actor or cli_actor())


def _human():
    """A human-only quick-task command: refused inside an agent harness or without a terminal."""
    from orch.actor import require_human_terminal
    from orch.core.events import Actor
    require_human_terminal("human-only action")
    return Actor("human", "you", "tty", None)


def line(t: dict) -> str:
    tail = []
    if t["status"] == "open" and t.get("outgrew"):
        tail.append("outgrew it")
    elif t["status"] == "open" and t.get("claimed_by"):
        tail.append(f"{t['claimed_by']}" + (" (stale)" if t.get("stale") else ""))
    elif t["status"] == "moved":
        tail.append(f"→ {t.get('ticket')}")
    elif t["status"] != "open":
        tail.append(t["status"])
    if t.get("area"):
        tail.append(t["area"])
    return f"{t['id']:<7} {t['title']}" + (f"   [{' · '.join(tail)}]" if tail else "")


def _state_line(cfg: dict) -> str:
    if cfg["enabled"]:
        return (f"quick tasks are on · limit {cfg['max_commits']} commit(s), {cfg['max_files']} file(s) · "
                f"agents {'may' if cfg['agents_add'] else 'may not'} add them")
    if cfg["state"] == "unsigned":
        return "quick tasks are off: config.json asks for them, but no signed decision backs it (`orch quick enable`)"
    return "quick tasks are off (`orch quick enable` in your own terminal)"


@quick_app.callback(invoke_without_command=True)
def quick_root(ctx: typer.Context,
               all_: Annotated[bool, typer.Option("--all", help="Also done, moved and dropped ones.")] = False,
               json_out: JsonOpt = False) -> None:
    """List the open quick tasks (with --all every one)."""
    if ctx.invoked_subcommand is not None:
        return
    _list(all_, json_out)


@quick_app.command("list")
def list_(all_: Annotated[bool, typer.Option("--all", help="Also done, moved and dropped ones.")] = False,
          json_out: JsonOpt = False) -> None:
    """List the open quick tasks (with --all every one)."""
    _list(all_, json_out)


def _list(all_: bool, json_out: bool) -> None:
    from orch.core import quick
    cli, ws = _ctx()
    cfg = quick.settings(ws)
    rows = [quick.view(ws, t, cfg) for t in quick.all_tasks(ws) if all_ or t["status"] == "open"]
    text = "\n".join([_state_line(cfg)] + [line(t) for t in rows] + ([] if rows else ["no open quick task"]))
    cli._out({"settings": cfg, "tasks": rows}, json_out, text)


@quick_app.command()
def show(ref: RefArg, json_out: JsonOpt = False) -> None:
    """One quick task: its line, who has it, the note and artifacts."""
    from orch.core import quick
    cli, ws = _ctx()
    t = quick.view(ws, quick.load(ws, ref))
    out = [line(t), f"  added   {t['added'].get('at')} by {t['added'].get('by')}"]
    if t.get("done"):
        out.append(f"  done    {t['done'].get('at')} by {t['done'].get('by')}: {t['done'].get('note') or '(no note)'}")
    if t.get("outgrew"):
        o = t["outgrew"]
        out.append(f"  outgrew {o.get('commits')} commit(s), {o.get('files_n')} file(s): {', '.join(o.get('files') or [])}")
    for n in t.get("notes") or []:
        out.append(f"  note    {n.get('at')} {n.get('by')}: {n.get('text')}")
    for a in t.get("artifacts") or []:
        out.append(f"  file    {a.get('name') or a.get('url')} ({a.get('kind')})" + (f" — {a['label']}" if a.get("label") else ""))
    cli._out(t, json_out, "\n".join(out))


@quick_app.command()
def add(title: Annotated[str, typer.Argument(help="The task, in one line.")],
        area: Annotated[Optional[str], typer.Option("--area", help="A file or folder it is about, e.g. docs/.")] = None,
        json_out: JsonOpt = False) -> None:
    """Add a quick task (agents only where the human allowed it)."""
    cli, ws = _ctx()
    t = _qops(ws).add(title, area)
    cli._out(t, json_out, f"added {t['id']}: {t['title']}")


@quick_app.command()
def claim(ref: RefArg, json_out: JsonOpt = False) -> None:
    """Take a quick task for this session (one at a time; never while your ticket is still in progress)."""
    cli, ws = _ctx()
    t = _qops(ws).claim(ref)
    cli._out(t, json_out, f"claimed {t['id']}: {t['title']}\n"
                          f"commit as `{t['id']} <summary>`, then `orch quick done {t['id']} -m \"what you did\"`")


@quick_app.command()
def release(ref: RefArg, json_out: JsonOpt = False) -> None:
    """Give a claimed quick task back."""
    cli, ws = _ctx()
    t = _qops(ws).release(ref)
    cli._out(t, json_out, f"released {t['id']}")


@quick_app.command()
def done(ref: RefArg, message: NoteOpt = None, json_out: JsonOpt = False) -> None:
    """Close a quick task with one line of proof. Refused past the size limit: the task is then marked outgrown."""
    cli, ws = _ctx()
    t = _qops(ws).done(ref, message)
    cli._out(t, json_out, f"{t['id']} done")


@quick_app.command()
def near(ref: Annotated[Optional[str], typer.Argument(metavar="TICKET", help="Your ticket; its commits give the files.")] = None,
         paths: Annotated[Optional[list[str]], typer.Option("--path", "-p", help="A file or folder (repeatable).")] = None,
         json_out: JsonOpt = False) -> None:
    """Open quick tasks in the files your ticket changed: what to pick up once that ticket is in testing."""
    from orch.actor import cli_actor
    from orch.core import quick
    from orch.errors import UsageError
    if not ref and not paths:
        raise UsageError("pass your ticket, --path, or both")
    cli, ws = _ctx()
    data = quick.near(ws, ref, list(paths or []), cli_actor())
    rows = data["tasks"]
    text = "\n".join(line(t) for t in rows) if rows else f"no open quick task in the {len(data['files'])} file(s)"
    cli._out(data, json_out, text)


@quick_app.command()
def promote(ref: RefArg, json_out: JsonOpt = False) -> None:
    """Make it a ticket: a backlog chore with the line as its title (its requirements still need the human)."""
    cli, ws = _ctx()
    t, ticket = _qops(ws).promote(ref)
    cli._out({"quick": t, "ticket": ticket.id}, json_out, f"{t['id']} is now {ticket.id} in backlog")


@quick_app.command()
def reopen(ref: RefArg, message: NoteOpt = None, json_out: JsonOpt = False) -> None:
    """Human only: send a done quick task back with why, or let an outgrown one finish."""
    cli, ws = _ctx()
    t = _qops(ws, _human()).reopen(ref, message)
    cli._out(t, json_out, f"{t['id']} is open again")


@quick_app.command()
def drop(ref: RefArg, json_out: JsonOpt = False) -> None:
    """Human only: drop a quick task."""
    cli, ws = _ctx()
    t = _qops(ws, _human()).drop(ref)
    cli._out(t, json_out, f"{t['id']} dropped")


@quick_app.command()
def enable(
    agents_add: Annotated[Optional[bool], typer.Option("--agents-add/--no-agents-add",
                                                       help="Whether agents may add quick tasks themselves.")] = None,
    max_commits: Annotated[Optional[int], typer.Option("--max-commits", help="Most commits a quick task may take.")] = None,
    max_files: Annotated[Optional[int], typer.Option("--max-files", help="Most files a quick task may change.")] = None,
    json_out: JsonOpt = False,
) -> None:
    """Human only: turn quick tasks on, signed into the approval ledger."""
    from orch.core import quick
    cli, ws = _ctx()
    cfg = quick.set_settings(ws, _human(), enabled=True, agents_add=agents_add, max_commits=max_commits,
                             max_files=max_files)
    cli._out(cfg, json_out, _state_line(cfg))


@quick_app.command()
def disable(json_out: JsonOpt = False) -> None:
    """Turn quick tasks off (anyone may)."""
    from orch.actor import cli_actor
    from orch.core import quick
    cli, ws = _ctx()
    cfg = quick.set_settings(ws, cli_actor(), enabled=False)
    cli._out(cfg, json_out, _state_line(cfg))


@quick_artifact_app.command("add")
def artifact_add(ref: RefArg,
                 file: Annotated[Optional[Path], typer.Argument(help="The file to attach.")] = None,
                 url: Annotated[Optional[str], typer.Option("--url", help="An http(s) link instead of a file.")] = None,
                 name: Annotated[Optional[str], typer.Option("--name", help="Store the file under this name.")] = None,
                 label: Annotated[Optional[str], typer.Option("--label", help="What it shows, in a few words.")] = None,
                 kind: Annotated[Optional[str], typer.Option("--kind", help="screenshot, report, log, …")] = None,
                 json_out: JsonOpt = False) -> None:
    """Attach a file or link to a quick task you hold (up to the workspace's limit, 5 by default)."""
    cli, ws = _ctx()
    t = _qops(ws).artifact_add(ref, file, url=url, name=name, label=label, kind=kind)
    a = t["artifacts"][-1]
    cli._out(t, json_out, f"{t['id']}: added {a.get('name') or a.get('url')}")
