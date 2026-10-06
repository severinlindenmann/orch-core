"""`orch worktree add|remove` (#164): a ticket's git worktree under `<root>/.claude/worktrees/<repo>/<slug>`."""
from __future__ import annotations

from typing import Annotated, Optional

import typer

worktree_app = typer.Typer(no_args_is_help=True, help="Git worktrees for a ticket, inside the workspace.")

JsonOpt = Annotated[bool, typer.Option("--json", help="Machine-readable output.")]
RepoOpt = Annotated[str, typer.Option("--repo", help="The repo (a git.repos name, or the workspace repo's name).")]


@worktree_app.command("add")
def add(ref: str, repo: RepoOpt,
        base: Annotated[Optional[str], typer.Option("--base", help="Start point of a new branch (default: the "
                                                    "repo's default_branch, else its HEAD).")] = None,
        json_out: JsonOpt = False) -> None:
    """Create the ticket's branch (git.branch_pattern) and worktree, link both, and link the harness files."""
    from orch import cli
    from orch.core import worktrees
    ws = cli._ws()
    r = worktrees.add(cli._ops(ws), ref, repo, base)
    text = f"{r['id']}: worktree {r['worktree']} on {'new ' if r['new_branch'] else ''}branch {r['branch']}"
    if r["harness"]:
        text += "\n  linked " + ", ".join(r["harness"])
    cli._out(r, json_out, text)


@worktree_app.command("remove")
def remove(ref: str, repo: RepoOpt, json_out: JsonOpt = False) -> None:
    """Remove the ticket's worktree in --repo and its link. The branch stays; a worktree with changes is refused."""
    from orch import cli
    from orch.core import worktrees
    ws = cli._ws()
    r = worktrees.remove(cli._ops(ws), ref, repo)
    cli._out(r, json_out, f"{r['id']}: removed worktree {r['worktree']} (branch kept)")
