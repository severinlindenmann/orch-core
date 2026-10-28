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
import os
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import TextIO

from .execute import Result, build_steps, execute
from .inventory import dev_markers, gather
from .model import Ctx, Inventory, Item, human_size
from .safe import agent_reason, tty_available

__all__ = ["main"]

AGENT_ENV = ("ORCH_GRANT", "ORCH_SESSION")
USAGE = (
    "usage: orch host prune [--dry-run] [--yes] [--workspaces ask|keep|delete] [--workspace PATH] [--keep-tool] "
    "[--i-know-dev] [--include-default-config]\n"
)


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="orch host prune", add_help=False, allow_abbrev=False)
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--yes", action="store_true")
    p.add_argument("--workspaces", choices=("ask", "keep", "delete"), default="ask")
    p.add_argument("--workspace", action="append", default=[])
    p.add_argument("--keep-tool", action="store_true")
    p.add_argument("--i-know-dev", action="store_true")
    p.add_argument("--include-default-config", action="store_true")
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


def _approve(ctx: Ctx, prompt: str) -> bool:
    from orch.ops.human import review_prompt

    return review_prompt(prompt.replace(" [y/N] ", ""), verb="continue")  # removes files: nothing is signed


def _workspaces(ctx: Ctx, inv: Inventory, yes: bool, mode: str, res_notes: list[str]) -> tuple[set[Path], set[Path]]:
    """``(folders to delete, workspaces to leave alone)``. Workspace data is never touched on ``--yes`` alone: orch's
    own files in a workspace (its store holds tickets, events and log keys that cannot be recovered) and a whole folder
    each need an answer typed at the person's own terminal (``/dev/tty``); without one they are kept."""
    delete: set[Path] = set()
    skip: set[Path] = set()
    for ws in inv.workspaces:
        if yes and ws.items:
            if not ctx.interactive:
                skip.add(ws.root)
                res_notes.append(f"{ws.root}: kept untouched (--yes does not cover workspaces; no terminal to ask)")
                continue
            if not _approve(
                ctx, f"Also remove orch's own files in {ws.root} (its tickets and events cannot be recovered)? [y/N] "
            ):
                skip.add(ws.root)
                continue
        if mode == "keep" or (mode == "ask" and yes):
            continue
        if ws.folder_refusal:
            ctx.say(f"Keeping the folder {ws.root}: {ws.folder_refusal}")
            continue
        if not ctx.interactive:
            res_notes.append(f"{ws.root}: folder kept (deleting a folder needs an answer at a terminal)")
            continue
        if _approve(
            ctx,
            f"Also delete the folder {ws.root} including its git history "
            f"({ws.files} files, {human_size(ws.size)})? [y/N] ",
        ):
            delete.add(ws.root)
    return delete, skip


def _tell_hubs(ctx: Ctx, inv: Inventory, folders: set[Path]) -> None:
    """Best effort, only with a person at the terminal: ``orch hub leave`` for each workspace with a hub, so the hub
    hears this computer is leaving. What cannot be done stays in the remote list of the report."""
    if not ctx.interactive:
        return
    for ws in inv.workspaces:
        if not ws.hub:
            continue
        joined = ctx.hub_status(ws.root, ctx.env)  # the live state, not the files a hub leaves behind
        prefix = f"workspace {(ws.wid or '')[:8]} has a hub"
        if joined is False:
            ctx.say(f"Workspace {ws.root}: left its hub earlier (nothing to tell it).")
            inv.remote[:] = [r for r in inv.remote if not r.startswith(prefix)]
            continue
        if joined is None:
            ctx.say(f"Workspace {ws.root}: could not read its hub status; the hub is not told (see the list below).")
            continue
        if not _approve(ctx, f"Workspace {ws.root} is on a hub. Tell the hub it leaves now (orch hub leave)? [y/N] "):
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
    elif res.skipped:
        say(f"\nDone: {len(res.done)} steps. {len(res.skipped)} item(s) could not be removed and were skipped.")
    else:
        say(
            f"\nDone: {len(res.done)} steps. orch is off this computer"
            + (" (except the notes below)." if res.admin or inv.remote else ".")
        )
    if res.skipped:
        say("\nSkipped (everything else was removed). Do this by hand:")
        for it, why, fix in res.skipped:
            say(f"  - {it.label or it.kind} {it.path}".rstrip())
            say(f"      why: {why[:200]}")
            say(f"      fix: {fix}")
    for note in res.notes:
        say(f"\nNote: {note}")
    if res.admin:
        say("\nStill to do by hand (needs an administrator or cannot be done by the program itself):")
        for c in res.admin:
            say(f"  {c}")
    if inv.remote:
        say("\nLeft on other computers (prune cannot delete these):")
        for r in inv.remote:
            say(f"  - {r}")
    say("\nNot removable by prune: browser data, git history in your folders, Claude Code's own records, backups.")
    return 1 if (res.failed is not None or res.skipped) else 0


def main(argv: Sequence[str], *, env: Mapping[str, str], out: TextIO, err: TextIO, ctx: Ctx | None = None) -> int:
    reason = agent_reason(env, ctx.euid if ctx else None)
    if reason:
        err.write(f"orch host prune: removing orch is a person's job; an agent never does it ({reason})\n")
        return 3
    try:
        a = _parser().parse_args(list(argv))
    except SystemExit:
        err.write(USAGE)
        return 2
    ctx = ctx or Ctx(env=env, out=out, err=err, interactive=tty_available())
    try:
        inv = gather(ctx, a.workspace, a.include_default_config)
    except Exception as e:  # noqa: BLE001 - the person gets a message, not a traceback
        err.write(f"orch host prune: could not look at this computer: {type(e).__name__}: {e}\n")
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
    if inv.blockers:
        for b in inv.blockers:
            err.write(f"orch host prune: {b}\n")
        return 2
    only_tool = n == 1 and any(i.kind == "tool" for i in inv.items)
    if n == 0:
        ctx.say("\nNothing to remove.")
        return 0
    if only_tool:
        ctx.say("\nNothing to remove except the orch tool itself.")
    if not a.yes:
        if not ctx.interactive:
            err.write("orch host prune: needs a terminal to ask; use --yes to run without asking (or --dry-run)\n")
            return 2
        question = "\nRemove the orch tool too? [y/N] " if only_tool else "\nRemove all of this? [y/N] "
        if not _approve(ctx, question):
            ctx.say("Nothing was removed.")
            return 5
    notes: list[str] = []
    for it in [i for i in inv.items if i.data.get("default_config")]:
        if not (
            ctx.interactive and _approve(ctx, f"Also remove {it.path}, a separate orch setup with its own keys? [y/N] ")
        ):
            inv.items.remove(it)
            inv.rmdirs[:] = [d for d in inv.rmdirs if os.fspath(d) != it.path]
            notes.append(f"{it.path}: kept (needs an answer at a terminal)")
    folders, skip = _workspaces(ctx, inv, a.yes, a.workspaces, notes)
    _tell_hubs(ctx, inv, folders)
    ctx.say("")
    res = Result()
    try:
        res = execute(ctx, inv, build_steps(inv, folders, skip))
    except BaseException as e:  # noqa: BLE001 - the report must always come
        res.error = f"{type(e).__name__}: {e}"
        res.failed = Item("", "internal", "the removal itself")
    res.notes[:0] = notes
    return report(ctx, inv, res)
