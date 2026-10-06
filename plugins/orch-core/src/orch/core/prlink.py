"""`orch link <id> --pr <number>` (#14): a PR/MR number becomes the URL of that review in the workspace repo's
`origin` remote. Local git only (`git remote get-url`); nothing is fetched."""
from __future__ import annotations

import re
import subprocess

from orch.errors import UsageError

_NUMBER = re.compile(r"#?(\d+)")
_URL = re.compile(r"(?:https?|ssh|git)://(?:[^@/\s]+@)?(?P<host>[^/:\s]+)(?::\d+)?/(?P<path>\S+?)(?:\.git)?/?")
_SCP = re.compile(r"(?:[^@/\s]+@)?(?P<host>[^:/\s]+):(?!//)(?P<path>\S+?)(?:\.git)?/?")


def pr_number(value: str) -> int | None:
    """The number of `--pr 22` / `--pr '#22'`, else None (a URL)."""
    m = _NUMBER.fullmatch(value.strip())
    return int(m.group(1)) if m else None


def parse_remote(url: str) -> tuple[str, str] | None:
    """(host, "owner/name") of a git remote URL (https, ssh:// or scp-like), or None."""
    url = (url or "").strip()
    m = _URL.fullmatch(url) or _SCP.fullmatch(url)
    if not m or "/" not in m.group("path"):
        return None
    return m.group("host").lower(), m.group("path")


def review_url(host: str, path: str, number: int, git_type: str = "github", base_url: str = "") -> str:
    """The review URL in the repo's git provider (#165: per repo). GitLab gets a merge request URL, Bitbucket Server
    and Bitbucket Cloud their pull-request forms; a github type keeps the host guess (a gitlab host gets the GitLab
    form), every other host the GitHub form (/pull/N)."""
    kind = (git_type or "github").lower()
    base = (base_url or f"https://{host}").rstrip("/")
    if kind.startswith("bitbucket-server") or kind in ("bitbucket-dc", "bitbucket-datacenter"):
        parts = path.split("/")
        if "scm" in parts:  # https clone URLs carry /scm/ (after any context path of base_url)
            parts = parts[len(parts) - parts[::-1].index("scm"):]
        if len(parts) >= 2:
            project, name = parts[-2], parts[-1]
            owner = f"users/{project[1:]}" if project.startswith("~") else f"projects/{project.upper()}"
            return f"{base}/{owner}/repos/{name}/pull-requests/{number}"
    if kind.startswith("bitbucket"):
        return f"{base}/{path}/pull-requests/{number}"
    if kind.startswith("gitlab") or (kind == "github" and "gitlab" in host):
        return f"{base}/{path}/-/merge_requests/{number}"
    return f"{base}/{path}/pull/{number}"


def _repos(ws):
    from orch.addons.api import workspace_repos
    return workspace_repos(ws)


def repo_of(ws, name: str | None):
    """The workspace repo `name` (orch.addons.api.RepoRef); without a name, the only repo of the workspace.
    Several repos and no name is refused: a guess could link the PR to the wrong repo."""
    repos = _repos(ws)
    if name:
        for r in repos:
            if r.name == name:
                return r
        raise UsageError(f"unknown repo {name!r}", hint="one of: " + ", ".join(r.name for r in repos))
    if len(repos) > 1:
        raise UsageError("this workspace has several repos; say which one the PR is in",
                         hint="pass --repo " + " | ".join(r.name for r in repos))
    return repos[0]


def origin(repo) -> tuple[str, str] | None:
    """(host, "owner/name") of the repo's own origin remote. A folder that is not a git repo itself gives None,
    so `git -C` never falls through to a parent repository."""
    if not (repo.path / ".git").exists():
        return None
    try:
        r = subprocess.run(["git", "-C", str(repo.path), "remote", "get-url", "origin"],
                           capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    return parse_remote(r.stdout) if r.returncode == 0 else None


def resolve(ws, repo_name: str | None, number: int) -> tuple[str, str]:
    """(repo name, review URL) for PR/MR `number` in that repo."""
    repo = repo_of(ws, repo_name)
    remote = origin(repo)
    if remote is None:
        raise UsageError(f"cannot resolve PR #{number}: repo {repo.name} has no usable origin remote",
                         hint="pass the PR/MR URL instead of the number")
    return repo.name, review_url(*remote, number, repo.git_type, repo.base_url)


def repo_for_url(ws, url: str) -> str:
    """The workspace repo whose origin the review URL belongs to (same host and owner/name)."""
    low = url.strip().lower()
    repos = _repos(ws)
    for repo in repos:
        remote = origin(repo)
        if remote is None:
            continue
        own = review_url(*remote, 0, repo.git_type, repo.base_url).rsplit("/", 1)[0] + "/"
        if low.startswith(f"https://{remote[0]}/{remote[1]}/".lower()) or low.startswith(own.lower()):
            return repo.name
    raise UsageError("no repo of this workspace has that PR's origin",
                     hint="pass --repo " + " | ".join(r.name for r in repos))
