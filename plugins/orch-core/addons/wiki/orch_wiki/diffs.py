"""branch-diffs: the files each testing/done ticket's branch changed, from local git through ctx.run in the
background (ruling R3). Never fetches, checks out or writes in the user's repos. When a branch is gone (merged
and deleted), the file list from an earlier check is kept."""
from __future__ import annotations

import re
from pathlib import Path

from orch.addons.api import Snapshot

from .gitcmd import GitError, run_git

REF = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,199}")
STATUSES = ("testing", "done")
MAX_TICKETS = 50
MAX_FILES = 2000


def _ref_ok(ref: str) -> bool:
    return bool(REF.fullmatch(ref)) and ".." not in ref and not ref.endswith((".lock", "/"))


def repo_dirs(ctx) -> dict[str, tuple[Path, str | None]]:
    root = Path(ctx.root)
    out = {}
    for name, spec in ((ctx.addon.ws.config.get("git") or {}).get("repos") or {}).items():
        if isinstance(spec, dict) and isinstance(spec.get("path"), str):
            default = spec.get("default_branch")
            out[str(name)] = ((root / spec["path"]).resolve(),
                              default if isinstance(default, str) and _ref_ok(default) else None)
    return out


class BranchDiffs:
    id = "branch-diffs"
    kind = "status"
    interval_s = 600

    def scopes(self, ctx) -> list[str]:
        return ["tickets"]

    def fetch(self, ctx, scope, previous):
        before = {i.get("id"): i for i in (previous.items if previous else ()) if isinstance(i, dict)}
        repos = repo_dirs(ctx)
        entries = [e for e in ctx.addon.tickets()
                   if e.status in STATUSES and isinstance((e.meta or {}).get("branches"), dict) and e.meta["branches"]]
        entries.sort(key=lambda e: str((e.meta or {}).get("updated") or ""), reverse=True)
        items = []
        for entry in entries[:MAX_TICKETS]:
            for repo, branch in sorted(entry.meta["branches"].items()):
                items.append(self._one(ctx, entry, str(repo), str(branch), repos, before.get(f"{entry.id}:{repo}")))
        message = f"checked the {MAX_TICKETS} most recently updated tickets" if len(entries) > MAX_TICKETS else ""
        return Snapshot(self.id, scope, ctx.now(), complete=not message, message=message, items=tuple(items))

    def _one(self, ctx, entry, repo: str, branch: str, repos: dict, before) -> dict:
        base = {"id": f"{entry.id}:{repo}", "label": entry.id, "ticket": entry.id, "status": entry.status,
                "repo": repo, "branch": branch}
        if repo not in repos:
            return {**base, "role": "neu", "text": f"repo {repo} is not in git.repos", "files": []}
        if not _ref_ok(branch):
            return {**base, "role": "neu", "text": "branch name not readable", "files": []}
        path, default = repos[repo]
        try:
            target = default or self._default_branch(ctx, path)
            ref = self._ref(ctx, path, branch)
            if ref is None:
                return self._keep(base, before, "branch not found locally")
            out = run_git(ctx, ["git", "-C", str(path), "diff", "--name-only", f"{target}...{ref}"]).stdout
        except GitError as e:
            return self._keep(base, before, e.message)
        files = [line.strip() for line in out.splitlines() if line.strip()][:MAX_FILES]
        if not files:
            return self._keep(base, before, f"no changes against {target}")
        return {**base, "role": "info", "text": f"{len(files)} file(s) changed in {repo}", "files": files, "base": target}

    def _keep(self, base: dict, before, why: str) -> dict:
        files = before.get("files") if isinstance(before, dict) else None
        if isinstance(files, list) and files:
            return {**before, **base, "role": "info", "files": files,
                    "text": f"{len(files)} file(s) from an earlier check ({why})"[:300]}
        return {**base, "role": "neu", "text": why[:300], "files": []}

    def _default_branch(self, ctx, path: Path) -> str:
        r = run_git(ctx, ["git", "-C", str(path), "symbolic-ref", "--quiet", "--short", "refs/remotes/origin/HEAD"],
                    ok=(0, 1, 128))
        name = r.stdout.strip()
        return name if r.returncode == 0 and _ref_ok(name) else "main"

    def _ref(self, ctx, path: Path, branch: str) -> str | None:
        for candidate in (branch, f"origin/{branch}"):
            r = run_git(ctx, ["git", "-C", str(path), "rev-parse", "--verify", "--quiet", f"{candidate}^{{commit}}"],
                        ok=(0, 1))
            if r.returncode == 0:
                return candidate
        return None
