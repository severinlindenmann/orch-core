"""``orch host prune``: take orch off this computer, as if it had never been installed.

    orch host prune [--dry-run] [--yes] [--workspaces ask|keep|delete] [--workspace PATH ...] [--keep-tool]
                    [--i-know-dev]

Step 1 always prints everything found on THIS computer (grouped, with paths and sizes). Nothing is removed before one
plain "Remove all of this? [y/N]" (``--yes`` skips it for scripts). Whole workspace folders are a separate question per
workspace (default No); ``--yes`` never includes them, only ``--workspaces delete`` does. A person's job: refused to
agents. Refused in the orch dev kit unless ``--i-know-dev``.
"""

from __future__ import annotations

import argparse
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import TextIO

from .execute import Result, build_steps, execute
from .inventory import dev_markers, gather
from .model import Ctx, Inventory, PruneError, human_size

__all__ = ["main"]

AGENT_ENV = ("ORCH_GRANT", "ORCH_SESSION")
USAGE = (
    "usage: orch host prune [--dry-run] [--yes] [--workspaces ask|keep|delete] [--workspace PATH] [--keep-tool] "
    "[--i-know-dev]\n"
)


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="orch host prune", add_help=False, allow_abbrev=False)
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--yes", action="store_true")
    p.add_argument("--workspaces", choices=("ask", "keep", "delete"), default="ask")
    p.add_argument("--workspace", action="append", default=[])
    p.add_argument("--keep-tool", action="store_true")
    p.add_argument("--i-know-dev", action="store_true")
    return p


def render(ctx: Ctx, inv: Inventory) -> int:
    """Print the inventory; returns the number of things that would be removed."""
    n = 0
    say = ctx.say
    say("orch host prune: what orch left on this computer")
    for group, items in inv.groups().items():
        say(f"\n{group}")
        for it in items:
            say(f"  - {it.line()}")
            n += it.kind != "note"
    for ws in inv.workspaces:
        say(f"\nWorkspace {ws.root}   (the folder: {ws.files} files, {human_size(ws.size)})")
        if not ws.items:
            say("  - nothing of orch's in it")
        for it in ws.items:
            say(f"  - {it.line()}")
            n += it.kind != "note"
        say("  - your own files and git history are kept unless you also choose to delete the folder")
    if inv.root_commands:
        say("\nAs an administrator (printed, or run when you start prune as root):")
        for c in inv.root_commands:
            say(f"  sudo {c}")
    if inv.remote:
        say("\nOn other computers (prune cannot delete these; it tells the hub when it can):")
        for r in inv.remote:
            say(f"  - {r}")
    if inv.warnings:
        say("\nNotes")
        for w in inv.warnings:
            say(f"  - {w}")
    say("\nNot removable by prune")
    for r in inv.not_removable:
        say(f"  - {r}")
    return n


def _confirm(ctx: Ctx, prompt: str) -> bool:
    return ctx.ask(prompt).strip().lower() in ("y", "yes")


def _folders(ctx: Ctx, inv: Inventory, mode: str) -> set[Path]:
    out: set[Path] = set()
    for ws in inv.workspaces:
        if ws.folder_refusal:
            if mode == "delete":
                ctx.say(f"Keeping the folder {ws.root}: {ws.folder_refusal}")
            continue
        if mode == "delete":
            out.add(ws.root)
        elif (
            mode == "ask"
            and ctx.interactive
            and _confirm(ctx, f"Also delete the folder {ws.root} ({ws.files} files, {human_size(ws.size)})? [y/N] ")
        ):
            out.add(ws.root)
    return out


def _tell_hubs(ctx: Ctx, inv: Inventory, folders: set[Path]) -> None:
    """Best effort, only with a person at the terminal: ``orch hub leave`` for each workspace with a hub, so the hub
    hears this computer is leaving. What cannot be done stays in the remote list of the report."""
    if not ctx.interactive:
        return
    for ws in inv.workspaces:
        if not ws.hub:
            continue
        if not _confirm(ctx, f"Workspace {ws.root} is on a hub. Tell the hub it leaves now (orch hub leave)? [y/N] "):
            continue
        ok, why = ctx.hub_leave(ws.root, ctx.env)
        ctx.say("  the hub was told" if ok else f"  could not tell the hub ({why}); it stays in the list below")
        if ok:
            inv.remote[:] = [r for r in inv.remote if not r.startswith(f"workspace {(ws.wid or '')[:8]} has a hub")]


def report(ctx: Ctx, inv: Inventory, res: Result) -> int:
    say = ctx.say
    if res.failed is not None:
        say(f"\norch host prune stopped: {res.error}")
        say(f"Done before it stopped: {len(res.done)} steps. Still on this computer:")
        for it in [res.failed, *res.left]:
            if it.label:
                say(f"  - {it.label} {it.path}".rstrip())
        say("Fix the problem and run `orch host prune` again; it only removes what is still there.")
    else:
        say(
            f"\nDone: {len(res.done)} steps. orch is off this computer"
            + (" (except the notes below)." if res.admin or inv.remote else ".")
        )
    if res.admin:
        say("\nStill to do by hand (needs an administrator or cannot be done by the program itself):")
        for c in res.admin:
            say(f"  {c}")
    if inv.remote:
        say("\nLeft on other computers (prune cannot delete these):")
        for r in inv.remote:
            say(f"  - {r}")
    say("\nNot removable by prune: browser data, git history in your folders, Claude Code's own records, backups.")
    return 1 if res.failed is not None else 0


def main(argv: Sequence[str], *, env: Mapping[str, str], out: TextIO, err: TextIO, ctx: Ctx | None = None) -> int:
    if any(env.get(k) for k in AGENT_ENV):
        err.write("orch host prune: removing orch is a person's job; an agent never does it\n")
        return 3
    try:
        a = _parser().parse_args(list(argv))
    except SystemExit:
        err.write(USAGE)
        return 2
    ctx = ctx or Ctx(env=env, out=out, err=err, interactive=_tty())
    if a.yes:
        ctx.interactive = (
            False  # nobody is asked anything: --yes never includes workspace folders (--workspaces delete does)
        )
    try:
        inv = gather(ctx, a.workspace)
    except PruneError as e:
        err.write(f"orch host prune: {e}\n")
        return 1
    if a.keep_tool:
        inv.items = [i for i in inv.items if i.kind != "tool"]
    n = render(ctx, inv)
    if a.dry_run:
        ctx.say("\n--dry-run: nothing was removed.")
        return 0
    why = dev_markers(ctx, inv.state)
    if why and not a.i_know_dev:
        err.write(
            "orch host prune: this looks like the orch dev kit ("
            + "; ".join(why)
            + "). Prune removes the install of THIS "
            "environment, so it is refused here. Run it in a clean environment (env -i), or pass --i-know-dev.\n"
        )
        return 4
    if n == 0:
        ctx.say("\nNothing to remove.")
        return 0
    if not a.yes:
        if not ctx.interactive:
            err.write("orch host prune: needs a terminal to ask; use --yes to run without asking (or --dry-run)\n")
            return 2
        if not _confirm(ctx, "\nRemove all of this? [y/N] "):
            ctx.say("Nothing was removed.")
            return 5
    folders = _folders(ctx, inv, a.workspaces)
    _tell_hubs(ctx, inv, folders)
    ctx.say("")
    res = execute(ctx, inv, build_steps(inv, folders))
    return report(ctx, inv, res)


def _tty() -> bool:
    import sys

    try:
        return sys.stdin.isatty()
    except (OSError, ValueError):
        return False
