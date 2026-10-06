"""`orch worktree add|remove` (#164): one git worktree per ticket and repo at `<root>/.claude/worktrees/<repo>/<slug>`.

Inside the workspace root, so `orch` finds `orchestrator/` by walking up and the factory runner accepts it as the
child's start folder. Harness files are linked entry by entry, never the whole `.claude` (that would loop)."""
from __future__ import annotations

import contextlib
import os
import subprocess
from pathlib import Path

from orch.errors import UsageError, ValidationError

WORKTREES_DIR = Path(".claude") / "worktrees"
# What each harness reads from the workspace's own folder; Copilot reads only files the repo itself tracks.
_CLAUDE_ENTRIES = (".claude/skills", ".claude/commands", ".claude/agents", ".claude/settings.json",
                   ".claude/settings.local.json", ".mcp.json")


def _git(cwd: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    try:
        r = subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.SubprocessError) as e:
        raise UsageError(f"git {args[0]} failed: {e}") from e
    if check and r.returncode != 0:
        raise UsageError(f"git {' '.join(args[:2])} failed: {(r.stderr or r.stdout).strip()}")
    return r


def _repo(ws, name: str):
    from orch.core.prlink import repo_of
    repo = repo_of(ws, name)
    if not (repo.path / ".git").exists():  # never let `git -C` fall through to a parent repository
        raise UsageError(f"repo {repo.name} ({repo.path}) is not a git checkout")
    return repo


def branch_name(ws, t) -> str:
    from orch.core.store import slugify
    pattern = ws.config["git"]["branch_pattern"]
    return pattern.replace("{key}", t.id).replace("{slug}", slugify(t.title or ""))


def harness_entries(cfg: dict) -> tuple[str, ...]:
    harnesses = set(cfg.get("harnesses") or [])
    return _CLAUDE_ENTRIES if harnesses & {"claude", "claude-plugin"} else ()


def _exclude(repo_dir: Path, lines: list[str]) -> None:
    """Add `lines` to the clone's info/exclude (shared by its worktrees, never committed)."""
    if not lines:
        return
    rel = _git(repo_dir, "rev-parse", "--git-path", "info/exclude").stdout.strip()
    path = Path(rel) if os.path.isabs(rel) else repo_dir / rel
    have = path.read_text(encoding="utf-8").splitlines() if path.is_file() else []
    new = [x for x in lines if x not in have]
    if new:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            if have and have[-1] != "":
                f.write("\n")
            f.write("\n".join(new) + "\n")


def link_harness(ws, wt: Path) -> list[str]:
    """Symlink each harness entry the workspace has into the worktree, unless the checkout already has its own."""
    root = Path(ws.root).resolve()
    made = []
    for entry in harness_entries(ws.config):
        src, dest = root / entry, wt / entry
        if not src.exists() or os.path.lexists(dest):
            continue
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.symlink_to(os.path.relpath(src, dest.parent))
        made.append(entry)
    _exclude(wt, ["/" + e for e in made])
    return made


def _unlink_harness(ws, wt: Path) -> None:
    """Remove only the symlinks `link_harness` made (they point at the workspace's own entry)."""
    root = Path(ws.root).resolve()
    for entry in harness_entries(ws.config):
        dest = wt / entry
        if dest.is_symlink() and os.path.realpath(dest) == os.path.realpath(root / entry):
            dest.unlink()


def add(ops, ref: str, repo_name: str, base: str | None = None) -> dict:
    from orch.core import store
    ws = ops.ws
    t = store.load(ws, ref)[1]
    repo = _repo(ws, repo_name)
    branch = branch_name(ws, t)
    if _git(repo.path, "check-ref-format", "--branch", branch, check=False).returncode != 0:
        raise ValidationError(f"git.branch_pattern gives an invalid branch name {branch!r}")
    existing = (t.meta.get("worktrees") or {}).get(repo.name)
    if existing:
        raise UsageError(f"{t.id} already has a worktree in {repo.name}: {existing}",
                         hint=f"orch worktree remove {t.id} --repo {repo.name}")
    root = Path(ws.root).resolve()
    rel = WORKTREES_DIR / repo.name / branch.rsplit("/", 1)[-1]
    wt = root / rel
    if os.path.lexists(wt):
        raise UsageError(f"{rel.as_posix()} already exists")
    if (root / ".git").exists():  # a single-repo workspace: keep the nested worktrees out of its status
        _exclude(root, ["/" + WORKTREES_DIR.as_posix() + "/"])
    wt.parent.mkdir(parents=True, exist_ok=True)
    has_branch = _git(repo.path, "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}", check=False).returncode == 0
    if has_branch:
        if base:
            raise UsageError(f"branch {branch} already exists; --base applies only to a new branch")
        _git(repo.path, "worktree", "add", str(wt), branch)
    else:
        _git(repo.path, "worktree", "add", "-b", branch, str(wt), base or repo.default_branch or "HEAD")
    made = link_harness(ws, wt)
    ops.link(t.id, repo=repo.name, branch=branch, worktree=rel.as_posix())
    return {"id": t.id, "repo": repo.name, "branch": branch, "worktree": rel.as_posix(), "path": str(wt),
            "new_branch": not has_branch, "harness": made}


def remove(ops, ref: str, repo_name: str) -> dict:
    """Remove the ticket's worktree in `repo_name` (only one under the worktrees folder) and its link; the branch
    stays. A worktree with changes is refused, never forced."""
    from orch.core import store
    ws = ops.ws
    t = store.load(ws, ref)[1]
    repo = _repo(ws, repo_name)
    rel = (t.meta.get("worktrees") or {}).get(repo.name)
    if not rel:
        raise UsageError(f"{t.id} has no worktree in {repo.name}")
    root = Path(ws.root).resolve()
    wt = (root / rel).resolve()
    base = (root / WORKTREES_DIR).resolve()
    if base not in wt.parents:
        raise UsageError(f"{rel} is not under {WORKTREES_DIR.as_posix()}; orch removes only worktrees it placed",
                         hint="remove it with `git worktree remove` yourself")
    if wt.exists():
        if _git(wt, "status", "--porcelain").stdout.strip():
            raise ValidationError(f"{rel} has uncommitted changes; nothing was removed",
                                  hint="commit or stash them first")
        _unlink_harness(ws, wt)
        _git(repo.path, "worktree", "remove", str(wt))
    _git(repo.path, "worktree", "prune", check=False)
    for d in (wt.parent, base):
        with contextlib.suppress(OSError):
            d.rmdir()  # only when empty
    ops.unlink_worktree(t.id, repo.name)
    return {"id": t.id, "repo": repo.name, "worktree": rel, "removed": True}

