"""Local git state of each repo for the repo cards (spec v2 §12): branch, ahead/behind the default branch,
uncommitted files, the origin remote and orch's commit check. `git -C <repo>` through ctx.run, in the background."""
from __future__ import annotations

import re
from pathlib import Path

from orch.addons.api import COMMIT_HOOK_HEADER, Snapshot
from orch.addons.runner import AddonRunError

_URL = re.compile(r"(?:https?|ssh|git)://(?:[^@/\s]+@)?(?P<host>[^/:\s]+)(?::\d+)?/(?P<path>\S+?)(?:\.git)?/?")
_SCP = re.compile(r"(?:[^@/\s]+@)?(?P<host>[^:/\s]+):(?!//)(?P<path>\S+?)(?:\.git)?/?")
_CHECK_ROLE = {"installed": "ok", "missing": "warn", "foreign": "warn", "unknown": "neu"}


def parse_remote(url) -> tuple[str, str] | None:
    """(host, "owner/name") of a git remote URL (https, ssh:// or scp-like), or None."""
    url = (url or "").strip()
    m = _URL.fullmatch(url) or _SCP.fullmatch(url)
    if not m or "/" not in m.group("path"):
        return None
    return m.group("host").lower(), m.group("path")


def git(ctx, repo, *args: str, timeout: float = 10.0):
    return ctx.run(["git", "-C", str(repo.path), *args], timeout=timeout)


def remote_of(ctx, repo) -> tuple[str, str] | None:
    r = git(ctx, repo, "remote", "get-url", "origin")
    return parse_remote(r.stdout) if r.returncode == 0 else None


def parse_status(stdout: str) -> tuple[str, int]:
    head, changed = "(unknown)", 0
    for line in stdout.splitlines():
        if line.startswith("# branch.head "):
            head = line.removeprefix("# branch.head ").strip()
        elif line and not line.startswith("#"):
            changed += 1
    return head, changed


def default_branch(ctx, repo) -> str | None:
    if repo.default_branch:
        return repo.default_branch
    r = git(ctx, repo, "symbolic-ref", "--short", "refs/remotes/origin/HEAD")
    name = r.stdout.strip() if r.returncode == 0 else ""
    return name.removeprefix("origin/") or None


def ahead_behind(ctx, repo, branch: str) -> tuple[int, int] | None:
    r = git(ctx, repo, "rev-list", "--left-right", "--count", f"origin/{branch}...HEAD")
    parts = r.stdout.split() if r.returncode == 0 else []
    if len(parts) != 2 or not all(p.isdigit() for p in parts):
        return None
    behind, ahead = int(parts[0]), int(parts[1])
    return ahead, behind


def commit_check(ctx, repo) -> str:
    r = git(ctx, repo, "rev-parse", "--git-path", "hooks")
    if r.returncode != 0 or not r.stdout.strip():
        return "unknown"
    hooks = Path(r.stdout.strip())
    hook = (hooks if hooks.is_absolute() else Path(repo.path) / hooks) / "commit-msg"
    try:
        text = hook.read_text(encoding="utf-8", errors="replace")
    except FileNotFoundError:
        return "missing"
    except OSError:
        return "unknown"
    return "installed" if text.split("\n", 2)[1:2] == [COMMIT_HOOK_HEADER] else "foreign"


class LocalGitProvider:
    id = "local-git"
    kind = "status"
    interval_s = 30

    def scopes(self, ctx):
        return [r.name for r in ctx.repos()]

    def fetch(self, ctx, scope, previous):
        at = ctx.now()
        repo = next((r for r in ctx.repos() if r.name == scope), None)
        if repo is None:
            return Snapshot(self.id, scope, at, health="error", message=f"{scope} is no longer in git.repos")
        try:
            st = git(ctx, repo, "status", "--porcelain=v2", "--branch")
            if st.returncode != 0:
                if "not a git repository" in st.stderr.lower():
                    return Snapshot(self.id, scope, at, items=(
                        {"id": "state", "label": "Repository", "role": "warn", "text": "not a git repository"},))
                first = (st.stderr.strip().splitlines() or [f"git status exited with {st.returncode}"])[0]
                return Snapshot(self.id, scope, at, health="error", message=first)
            head, changed = parse_status(st.stdout)
            remote = remote_of(ctx, repo)
            branch = default_branch(ctx, repo)
            ab = ahead_behind(ctx, repo, branch) if branch else None
            hook = commit_check(ctx, repo)
        except AddonRunError as e:
            return Snapshot(self.id, scope, at, health="error", message=e.message)
        items = (
            {"id": "branch", "label": "Branch", "role": "info", "text": head},
            {"id": "vs-default", "label": f"vs {branch}" if branch else "vs default branch",
             "role": "warn" if ab and ab[1] else "neu", "text": f"ahead {ab[0]}, behind {ab[1]}" if ab else "unknown",
             "ahead": ab[0] if ab else None, "behind": ab[1] if ab else None},
            {"id": "changes", "label": "Uncommitted", "role": "warn" if changed else "ok",
             "text": (f"{changed} file" + ("s" if changed != 1 else "")) if changed else "clean", "count": changed},
            {"id": "remote", "label": "Remote", "role": "neu", "text": f"{remote[0]}/{remote[1]}" if remote else "no origin remote",
             "host": remote[0] if remote else None, "full_name": remote[1] if remote else None},
            {"id": "commit-check", "label": "Commit check", "role": _CHECK_ROLE[hook], "text": hook},
        )
        return Snapshot(self.id, scope, at, items=items)
