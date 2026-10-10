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


_ENV_ALLOW = ("PATH", "HOME", "LANG", "TMPDIR", "TERM")


def git_env(base: Mapping[str, str] | None = None) -> dict[str, str]:
    """The environment git runs in: an allow-list (``PATH``, ``HOME``, ``LANG``, ``LC_*``, ``TMPDIR``, ``TERM``). No
    ``GIT_*`` variable (a caller cannot choose the repository, the index or a credential helper through them) and no
    token, key or ``ORCH_*`` variable reaches git or the hooks and helpers it may start."""
    src = os.environ if base is None else base
    return {
        k: v for k, v in src.items() if (k in _ENV_ALLOW or k.startswith("LC_")) and not k.upper().startswith("GIT_")
    }


def git(path: Path, *args: str) -> str | None:
    """``git -C path ...`` on a scrubbed environment; stdout stripped, ``None`` if git fails or is missing."""
    try:
        done = subprocess.run(
            [
                "git",
                "-c",
                "core.fsmonitor=false",
                "-c",
                "core.attributesFile=/dev/null",
                "--no-optional-locks",
                "-C",
                str(path),
                *args,
            ],
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


def dirty(path: Path) -> str | None:
    """Best effort: what a person would call uncommitted work. ``git status --porcelain`` (untracked files included)
    plus
    the files git is told to ignore changes of (``skip-worktree`` and ``assume-unchanged``, from ``git ls-files -v``).
    Empty means clean as far as this can tell; ``None`` means git could not be asked. It cannot see everything (files
    matched by ``.gitignore`` or ``info/exclude``, a ``filter`` in the repository's own config): a receipt is
    attested by
    the agent's environment anyway (F1 10.7)."""
    status = porcelain(path)
    listing = git(path, "ls-files", "-v")
    if status is None or listing is None:
        return None
    hidden = [ln[2:] for ln in listing.splitlines() if ln[:1] == "S" or ln[:1].islower()]
    return status + ("\n" if status and hidden else "") + "\n".join(f"hidden change flag: {h}" for h in hidden)


def repo_path(root: Path, repos: Mapping[str, str], name: str) -> Path | None:
    """The working copy of repo ``name`` (``settings.repos``, absolute or relative to the workspace directory)."""
    p = repos.get(name)
    return (root / p).resolve() if p else None


def repo_identity(path: Path, name: str) -> str:
    """The canonical identity of the repository (F1 5.7): its ``origin`` remote as an ``https://`` URL when that is
    canonical, else ``local:<name>``. Credentials in the remote (``https://user:token@host/...``) are stripped before
    the URL is looked at and never stored, printed or put on a command line; a remote that is not canonical after
    that is ``local:<name>``, never the raw URL."""
    url = git(path, "remote", "get-url", "origin") or ""
    url = re.sub(r"^(https?://)[^/@]*@", r"\1", url)  # userinfo (a user:token pair) is dropped before anything else
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


def observe(store: Any, ref: str, problems: list[str] | None = None) -> list[dict[str, Any]]:
    """Append the ``branch.pushed`` (host) events the ticket ``ref`` is owed: for each linked repo that has a branch in
    ``links.branches``, compare the branch head in git with the source-list entry and append when they differ (the first
    sighting has ``before: null``). When an append voids ``verify`` or ``code`` approvals, the host's
    ``gate.invalidated`` (``new_commits``) follows, so a waiting agent hears of it. Returns the events appended.

    git runs **without the workspace lock** (a slow or hostile repository cannot hold every writer up); only the append
    takes it, and the model re-checks ``before`` there (a head that moved meanwhile is refused and looked at next time).
    A read-only store, a repo without a working copy, a branch git does not know and a ref that names no commit object
    are skipped; the last is added to ``problems`` (never signed). Call it before ``submit``, ``show`` and ``wait`` (and
    a person's approval prompt, C7). In P1 the source list is only as trustworthy as the working copy the agent can
    write."""
    from .errors import StoreError

    if not store.can_write:
        return []
    view = store.ticket(ref)
    if view is None or view.status in ("closed",):
        return []
    links = view.fields["links"]
    wanted = []
    for name in links["repos"]:  # git, with no lock held
        branch = links["branches"].get(name)
        path = repo_path(store.root, store.state.workspace.repos, name)
        if not branch or path is None or not path.is_dir():
            continue
        ref_name = f"refs/heads/{branch}"
        if git(path, "rev-parse", "--verify", "-q", ref_name) is None:
            continue
        sha = git(path, "rev-parse", "--verify", "-q", f"{ref_name}^{{commit}}")
        if not sha or not _HEX.fullmatch(sha):
            if problems is not None:
                problems.append(f"{name}: {ref_name} names no commit object; nothing was recorded")
            continue
        wanted.append((name, ref_name, sha, repo_identity(path, name)))
    out = []
    for name, ref_name, sha, rid in wanted:
        view = store.ticket(ref) or view
        cur = next((dict(e) for e in view.source_list if e["repo"] == rid), None)
        if cur is not None and cur["ref"] == ref_name and cur["sha"] == sha:
            continue
        before = {"repo_id": cur["repo"], "ref": cur["ref"], "sha": cur["sha"]} if cur else None
        counting = {
            g: {d.id for d in view.gates[g].decisions if d.counting} for g in ("verify", "code") if g in view.gates
        }
        payload = {"repo_name": name, "repo_id": rid, "ref": ref_name, "sha": sha, "before": before}
        try:
            out.append(store.host_append("branch.pushed", view.uid, payload))
        except StoreError:  # the head moved since we looked (or the ticket no longer takes it): next time
            continue
        view = store.ticket(ref) or view
        for g, ids in counting.items():
            voided = sorted(d.id for d in view.gates[g].decisions if not d.counting and d.id in ids)
            if voided:
                try:
                    out.append(
                        store.host_append(
                            "gate.invalidated", view.uid, {"gate": g, "cause": "new_commits", "voided": voided}
                        )
                    )
                except StoreError:
                    pass
        view = store.ticket(ref) or view
    return out
