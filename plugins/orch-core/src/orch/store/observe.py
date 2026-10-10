"""Reading the linked git repositories (F1 5.7 "the host appends any pending ``branch.pushed``", D58).

One place that talks to git for the core: ``orch.ops.runner`` (the commit and the cleanliness of the tree around a
``task done --run``) and :func:`observe` (the source list of a ticket). Every git call runs with a **scrubbed
environment**: all ``GIT_*`` variables are dropped (a caller cannot choose the repository, the index or the object store
through them), ``core.fsmonitor`` is off and nothing is read from the user's terminal.
"""

from __future__ import annotations

import os
import re
import subprocess
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from orch import canon

__all__ = ["git", "git_env", "head", "observe", "porcelain", "repo_identity", "repo_path"]

_HEX = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")
_TIMEOUT = 30


def git_env(base: Mapping[str, str] | None = None) -> dict[str, str]:
    """``base`` (default: ``os.environ``) without any ``GIT_*`` variable."""
    return {k: v for k, v in (os.environ if base is None else base).items() if not k.upper().startswith("GIT_")}


def git(path: Path, *args: str) -> str | None:
    """``git -C path ...`` on a scrubbed environment; stdout stripped, ``None`` if git fails or is missing."""
    try:
        done = subprocess.run(
            ["git", "-c", "core.fsmonitor=false", "-C", str(path), *args],
            capture_output=True,
            text=True,
            timeout=_TIMEOUT,
            check=False,
            env={**git_env(), "GIT_TERMINAL_PROMPT": "0", "GIT_OPTIONAL_LOCKS": "0"},
            stdin=subprocess.DEVNULL,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return done.stdout.strip() if done.returncode == 0 else None


def head(path: Path) -> str | None:
    """The full commit id of ``HEAD``, or ``None``."""
    out = git(path, "rev-parse", "HEAD")
    return out if out and _HEX.fullmatch(out) else None


def porcelain(path: Path) -> str | None:
    """``git status --porcelain`` (empty: a clean tree, untracked files count); ``None`` if git fails."""
    return git(path, "status", "--porcelain=v1", "--untracked-files=normal")


def repo_path(root: Path, repos: Mapping[str, str], name: str) -> Path | None:
    """The working copy of repo ``name`` (``settings.repos``, absolute or relative to the workspace directory)."""
    p = repos.get(name)
    return (root / p).resolve() if p else None


def repo_identity(path: Path, name: str) -> str:
    """The canonical identity of the repository (F1 5.7): its ``origin`` remote as an ``https://`` URL when that is
    canonical, else ``local:<name>``."""
    url = git(path, "remote", "get-url", "origin") or ""
    m = re.fullmatch(r"(?:ssh://)?git@([^:/]+)[:/](.+)", url)
    if m:
        url = f"https://{m.group(1)}/{m.group(2)}"
    url = re.sub(r"\.git$", "", url, flags=re.I).rstrip("/")
    host, sep, rest = url.removeprefix("https://").partition("/")
    if url.startswith("https://") and sep:
        url = f"https://{host.lower()}/{rest}"
    try:
        return canon.check_repo_identity(url)
    except canon.HashError:
        return f"local:{name}"


def observe(store: Any, ref: str) -> list[dict[str, Any]]:
    """Append the ``branch.pushed`` (host) events the ticket ``ref`` is owed: for each linked repo that has a branch in
    ``links.branches``, compare the branch head in git with the source-list entry and append when they differ (the first
    sighting has ``before: null``). Returns the events appended. A read-only store, a repo without a working copy and a
    branch git does not know are skipped. Call it before ``submit``, ``show`` and ``wait`` (and a person's approval
    prompt, C7), so what is shown and decided is the code that is there."""
    if not store.can_write:
        return []
    view = store.ticket(ref)
    if view is None or view.status in ("closed",):
        return []
    links = view.fields["links"]
    out = []
    for name in links["repos"]:
        branch = links["branches"].get(name)
        path = repo_path(store.root, store.state.workspace.repos, name)
        if not branch or path is None or not path.is_dir():
            continue
        ref_name = f"refs/heads/{branch}"
        sha = git(path, "rev-parse", "--verify", "-q", ref_name)
        if not sha or not _HEX.fullmatch(sha):
            continue
        rid = repo_identity(path, name)
        cur = next((dict(e) for e in view.source_list if e["repo"] == rid), None)
        if cur is not None and cur["ref"] == ref_name and cur["sha"] == sha:
            continue
        before = {"repo_id": cur["repo"], "ref": cur["ref"], "sha": cur["sha"]} if cur else None
        payload = {"repo_name": name, "repo_id": rid, "ref": ref_name, "sha": sha, "before": before}
        out.append(store.host_append("branch.pushed", view.uid, payload))
        view = store.ticket(ref) or view
    return out
