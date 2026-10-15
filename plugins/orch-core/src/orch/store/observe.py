"""Reading the linked git repositories (F1 5.7 "the host appends any pending ``branch.pushed``", D58).

One place that talks to git for the core: ``orch.ops.runner`` (the commit and the cleanliness of the tree around a
``task done --run``) and :func:`observe` (the source list of a ticket). Every git call meets the invariant of
:mod:`orch.gitsafe` (:data:`orch.gitsafe.ISOLATION`, #546): no configuration the person or the repository controls is
read except through an explicit allow-list.

* **A closed set of operations** (:data:`orch.gitsafe.OPS`): :func:`git` takes an operation name and at most one
  validated operand (a branch ref), never free arguments, so no caller can add ``-c``, ``-C``, ``--git-dir`` or an
  option of its own (Codex P1 on #561).
* **The repository's own configuration:** :func:`orch.gitsafe.command` runs :func:`orch.gitsafe.gate` before every
  operation, with no exception and no fallback (a key outside the allow-list, such as a ``filter.<d>.clean`` that a
  ``.gitattributes`` selects, or a config file another account can write: git is not started and the answer is
  ``None``, "cannot be told"). ``status`` never opens a submodule (``--ignore-submodules=all``) and refuses a
  populated one (:func:`orch.gitsafe.submodules`); the operation enforces both, not the caller.
* **The person's configuration:** system and global configuration off; ``status`` keeps only
  :data:`orch.gitsafe.STATUS_KEYS`, every other operation nothing.
* **The environment** is built from scratch (:func:`orch.gitsafe.isolated_env`): no ``GIT_*`` variable of the caller
  (it cannot choose the repository, the index or the object store through them), no token, no ``ORCH_*``. Hooks,
  fsmonitor, the network, pagers and diff programs are off, and git has no controlling terminal.
"""

from __future__ import annotations

import os
import re
import subprocess
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from orch import canon, gitsafe

__all__ = [
    "dirty", "git", "head", "observe", "porcelain", "refusal", "repo_identity", "repo_path", "symbolic_head",
    "toplevel",
]  # fmt: skip

_HEX = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")
_TIMEOUT = 30


def git(path: Path, op: str, *, rev: str | None = None, strip: bool = True) -> str | None:
    """Operation ``op`` of :data:`orch.gitsafe.OPS` against the repository at ``path``, isolated
    (:func:`orch.gitsafe.command`: the gate first, every time; a populated submodule refused for ``status``; system
    and global configuration off). ``rev``: the operation's revision operand (``HEAD`` or ``refs/heads/<name>``,
    validated). Stdout stripped (unless ``strip`` is off). ``None`` if the operation or operand is refused, the gate
    refuses, git fails or is missing: git is never started after a refusal (fail closed)."""
    own = os.environ
    try:
        argv, env = gitsafe.command(os.path.abspath(path), op, own, operand=rev)
        done = subprocess.run(
            argv,
            capture_output=True,
            encoding="utf-8",
            errors="surrogateescape",
            timeout=_TIMEOUT,
            check=False,
            env=env,
            cwd="/",
            stdin=subprocess.DEVNULL,
            start_new_session=True,  # no controlling terminal: nothing git starts can reach the person's tty
        )
    except (gitsafe.Unsafe, OSError, subprocess.SubprocessError):
        return None
    if done.returncode != 0:
        return None
    return done.stdout.strip() if strip else done.stdout


def head(path: Path) -> str | None:
    """The full commit id of ``HEAD``, or ``None``."""
    out = git(path, "head")
    return out if out and _HEX.fullmatch(out) else None


def refusal(path: Path) -> str | None:
    """Why orch does not run git in the repository at ``path`` (:func:`orch.gitsafe.gate`'s reason), or ``None`` (no
    refusal, or no repository at all: no ``.git`` at ``path`` or above it)."""
    here = Path(os.path.abspath(path))
    if not any(os.path.lexists(d / ".git") for d in (here, *here.parents)):
        return None
    try:
        gitsafe.gate(os.path.abspath(path), os.environ)
    except gitsafe.Unsafe as exc:
        return exc.reason
    return None


def symbolic_head(path: Path) -> str | None:
    """The branch ref ``HEAD`` names (``refs/heads/<branch>``), ``None`` when detached or unreadable."""
    return git(path, "symbolic_head")


def toplevel(path: Path) -> str | None:
    """The top of the working copy ``path`` is in, or ``None``."""
    return git(path, "toplevel")


def porcelain(path: Path) -> str | None:
    """``git status --porcelain`` (empty: a clean tree, untracked files count); ``None`` if git fails, the gate
    refuses or a submodule is checked out."""
    return git(path, "status")


def dirty(path: Path) -> str | None:
    """Best effort: what a person would call uncommitted work. ``git status --porcelain`` (untracked files included),
    a staged gitlink change (``status`` never looks at submodules), and the files git is told to ignore changes of
    (``skip-worktree`` and ``assume-unchanged``, from ``git ls-files -v``). Empty means clean as far as this can tell;
    ``None`` means git could not be asked, or the repository's own configuration is outside orch's allow-list, or it
    has a checked-out submodule (:mod:`orch.gitsafe`). It cannot see everything (files matched by ``.gitignore`` or
    ``info/exclude``): a receipt is attested by the agent's environment anyway (F1 10.7)."""
    status = porcelain(path)
    staged = git(path, "staged_raw", strip=False)
    listing = git(path, "index_flags")
    if status is None or staged is None or listing is None:
        return None
    lines = [status] if status else []
    lines += [f"staged submodule change: {p}" for p in gitsafe.gitlinks(staged.encode("utf-8", "surrogateescape"))]
    lines += [f"hidden change flag: {ln[2:]}" for ln in listing.splitlines() if ln[:1] == "S" or ln[:1].islower()]
    return "\n".join(lines)


def repo_path(root: Path, repos: Mapping[str, Mapping[str, str]], name: str) -> Path | None:
    """The working copy of repo ``name`` (``settings.repos``, absolute or relative to the workspace directory)."""
    p = repos.get(name)
    return (root / p["path"]).resolve() if p else None


def repo_identity(path: Path, name: str) -> str:
    """The identity of the repository (F1 5.7) from the raw ``remote.origin.url`` (no ``insteadOf``, unstripped): the
    canonical ``https://`` URL it maps to, else ``local:<name>``; more than one value is ``local:<name>`` (git fetches
    from the first and pushes to all).

    Read from the repository's own configuration as the gate lists it (:func:`orch.gitsafe.local_config`: isolated,
    ``--no-includes``, executes nothing), not by a second git command, so a repository the gate would refuse (say, a
    local ``url.<base>.insteadOf``) still has its identity (F1 5.7) and nothing runs. A listing that fails is
    ``local:<name>``."""
    try:
        entries = gitsafe.local_config(os.path.abspath(path), os.environ)
    except gitsafe.Unsafe:
        entries = []
    values = [v for _scope, k, v in entries if k == "remote.origin.url" and v]
    return canon.canonical_repo_identity(values[0] if len(values) == 1 else "", name)


def observe(store: Any, ref: str, problems: list[str] | None = None, backend: Any = None) -> list[dict[str, Any]]:
    """Append the ``branch.pushed`` (host) events the ticket ``ref`` is owed: for each linked repo that has a branch in
    ``links.branches``, compare the branch head in git with the source-list entry and append when they differ (the first
    sighting has ``before: null``). When an append voids ``verify`` or ``code`` approvals, the host's
    ``gate.invalidated`` (``new_commits``) follows, so a waiting agent hears of it. Returns the events appended.

    git runs **without the workspace lock** (a slow or hostile repository cannot hold every writer up); only the append
    takes it, and the model re-checks ``before`` there (a head that moved meanwhile is refused and looked at next time).
    A read-only store, a repo without a working copy, a branch git does not know and a ref that names no commit object
    are skipped; the last is added to ``problems`` (never signed). Call it before ``submit``, ``show`` and ``wait`` (and
    a person's approval prompt, C7). In P1 the source list is only as trustworthy as the working copy the agent can
    write.

    ``backend`` (host mode): orch's reader instead of the git CLI, ``look(path, name, ref) -> (sha | None, identity)``
    (:mod:`orch.gitread`); ``None`` keeps the git CLI calls exactly as in P1."""
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
        path = (backend.repo_path if backend is not None else repo_path)(store.root, store.state.workspace.repos, name)
        if not branch:
            continue  # the ticket names no branch yet: submit says so itself
        if path is None or not path.is_dir():
            if problems is not None:
                problems.append(f"{name}: no working copy in settings.repos; its code cannot be observed")
            continue
        ref_name = f"refs/heads/{branch}"
        if backend is not None:
            from orch.gitread.errors import Unavailable

            try:
                found, rid = backend.look(path, name, ref_name)
            except Unavailable as e:
                if problems is not None:
                    problems.append(f"{name}: {e.reason}; its code cannot be observed")
                continue
            if found is None:
                if problems is not None:
                    problems.append(f"{name}: git does not know {ref_name}; its code cannot be observed")
                continue
            sha = found
        else:
            if git(path, "ref", rev=ref_name) is None:
                if problems is not None:
                    problems.append(f"{name}: git does not know {ref_name}; its code cannot be observed")
                continue
            sha = git(path, "commit", rev=ref_name)
            if not sha or not _HEX.fullmatch(sha):
                if problems is not None:
                    problems.append(f"{name}: {ref_name} names no commit object; nothing was recorded")
                continue
            rid = repo_identity(path, name)
        declared = store.state.workspace.repos[name].get("remote")
        if declared is not None and not canon.same_repo_identity(declared, rid):
            if problems is not None:
                problems.append(f"{name}: repo.remote_mismatch; its code cannot be observed")
            continue
        wanted.append((name, ref_name, sha, declared if declared is not None else rid))
    out = []
    for name, ref_name, sha, rid in wanted:
        view = store.ticket(ref) or view
        cur = next((dict(e) for e in view.source_list if canon.same_repo_identity(e["repo"], rid)), None)
        if cur is not None and cur["ref"] == ref_name and cur["sha"] == sha:
            continue
        before = {"repo_id": cur["repo"], "ref": cur["ref"], "sha": cur["sha"]} if cur else None
        payload = {"repo_name": name, "repo_id": rid, "ref": ref_name, "sha": sha, "before": before}
        try:
            out += store.host_append("branch.pushed", view.uid, payload)
        except StoreError as e:  # the head moved since we looked (``source.not_new``) is normal: next time
            if e.code != "source.not_new" and problems is not None:
                problems.append(f"{name}: the observation was refused ({e.code})")
            continue
        view = store.ticket(ref) or view
    try:  # whatever the model voided without a record (an earlier crash, a changed section) is recorded now
        out += store.flush_invalidations(ref)
    except StoreError as e:
        if problems is not None:
            problems.append(f"a gate invalidation was refused ({e.code}); orch doctor can look at the log")
    return out
