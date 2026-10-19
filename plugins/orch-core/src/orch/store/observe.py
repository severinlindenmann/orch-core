"""Reading the linked git repositories (F1 5.7 "the host appends any pending ``branch.pushed``", D58).

One place that reads git for the core in direct mode: ``orch.ops.runner`` (the commit and the cleanliness of the tree
around a ``task done --run``), ``orch.ops.caps.Git``, ``hostclient``, the dashboard's dev host and :func:`observe`
(the source list of a ticket).

**No git process (#565).** Every read here goes through :data:`orch.gitread.local.READER`, orch's own reader
in-process: the repository's config is read as data (three keys; nothing in it is acted on), and no filter,
attribute, hook, fsmonitor or diff driver is ever read, so no configuration the person or the repository controls can
make a read run a program, and there is no window between a check and a git process for a concurrent writer of
``.git/config`` to race (before #565, :func:`orch.gitsafe.gate` approved the config and git started later; a
``filter.<x>.clean`` added in between ran under ``status``). What the reader cannot parse (a split or sparse index, an
extension, reftable, a populated submodule, ...) is ``None``: "cannot be told", never clean.
"""

from __future__ import annotations

import base64
import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from orch import canon
from orch.gitread.errors import Unavailable
from orch.gitread.local import READER
from orch.layout import Layout

__all__ = [
    "dirty", "dirty_refusal", "head", "observe", "refusal", "repo_identity", "repo_path", "symbolic_head", "toplevel",
    "tree_state",
]  # fmt: skip


def head(path: Path) -> str | None:
    """The full commit id of ``HEAD`` (a branch's tip or a detached commit), or ``None`` (an unborn branch, or the
    repository cannot be read)."""
    try:
        return READER.head(path)[1]
    except Unavailable:
        return None


def refusal(path: Path) -> str | None:
    """Why orch cannot read the repository at ``path`` (the reader's reason), or ``None`` (it can, or there is no
    repository at all: no ``.git`` at ``path`` or above it)."""
    try:
        READER.head(path)
    except Unavailable as exc:
        here = Path(os.path.abspath(path))
        if not any(os.path.lexists(d / ".git") for d in (here, *here.parents)):
            return None
        return exc.reason
    return None


def symbolic_head(path: Path) -> str | None:
    """The branch ref ``HEAD`` names (``refs/heads/<branch>``), ``None`` when detached or unreadable."""
    try:
        return READER.head(path)[0]
    except Unavailable:
        return None


def toplevel(path: Path) -> str | None:
    """The top of the working copy ``path`` is in, or ``None`` (none, or one the reader refuses)."""
    try:
        return READER.toplevel(path)
    except Unavailable:
        return None


def dirty(path: Path, env: Mapping[str, str] | None = None) -> str | None:
    """What a person would call uncommitted work in the whole repository ``path`` is in (from its top): staged and
    unstaged changes, untracked files (``.gitignore``, ``info/exclude`` and the person's global ignore file apply),
    a submodule whose recorded commit, type or presence changed, and the files git is told to ignore changes of
    (``skip-worktree``, ``assume-unchanged``), one ``why: path`` line each. ``""`` means clean; ``None`` means it
    cannot be told (the reader refused something, a checked-out submodule, a limit). It cannot see everything (files
    matched by an ignore file): a receipt is attested by the agent's environment anyway (F1 10.7)."""
    try:
        return READER.dirty(path, env)
    except Unavailable:
        return None


def dirty_refusal(path: Path, env: Mapping[str, str] | None = None) -> str | None:
    """Why :func:`dirty` cannot tell (the reader's reason, e.g. a checked-out submodule or a split index), or
    ``None`` when it can. For messages only: it reads the tree again."""
    try:
        READER.dirty(path, env)
    except Unavailable as exc:
        return exc.reason
    return None


def tree_state(
    path: Path, *, pins: Any = None, env: Mapping[str, str] | None = None
) -> tuple[dict[str, Any] | None, str | None]:
    """HEAD and the tree in ONE read (#583, L4): ``({head, head_ref, dirty}, None)``, ``dirty`` as :func:`dirty`
    answers it, or ``(None, reason)`` when it cannot be told. ``pins``: a registered worktree's
    :class:`~orch.gitread.fs.RepoPins`, read exactly (never found again by path)."""
    from orch.gitread.treejob import escape_path

    try:
        raw = READER.tree_check(path, env, pins=pins)
    except Unavailable as exc:
        return None, exc.reason
    lines = [f"{c['why']}: {escape_path(base64.b64decode(c['path_b64']), c['cut'])}" for c in raw["changed"]]
    if raw["more"]:
        lines.append("and more")
    return {"head": raw["head"], "head_ref": raw["head_ref"], "dirty": "\n".join(lines)}, None


def repo_path(layout: Layout, repos: Mapping[str, Mapping[str, str]], name: str) -> Path | None:
    """The working copy of repo ``name`` (``settings.repos``, absolute or relative to the PROJECT root; never to the
    store directory, which takes the layout so a bare root cannot be passed)."""
    p = repos.get(name)
    return (layout.project / p["path"]).resolve() if p else None


def repo_identity(path: Path, name: str) -> str:
    """The identity of the repository (F1 5.7) from the raw ``remote.origin.url`` (no ``insteadOf``, unstripped): the
    canonical ``https://`` URL it maps to, else ``local:<name>``; more than one value is ``local:<name>`` (git fetches
    from the first and pushes to all). Read by orch's own config parser (#565: no git process); a repository the
    reader cannot open is ``local:<name>``."""
    return READER.identity(path, name)


class _Local:
    """``observe``'s backend in direct mode: the in-process reader, one open per repo for the ref and the identity."""

    @staticmethod
    def look(path: Path, name: str, ref_name: str) -> tuple[str | None, str]:
        sha = READER.commit(path, ref_name)
        return sha, READER.identity(path, name)


_LOCAL = _Local()


def observe(store: Any, ref: str, problems: list[str] | None = None, backend: Any = None) -> list[dict[str, Any]]:
    """Append the ``branch.pushed`` (host) events the ticket ``ref`` is owed: for each linked repo that has a branch in
    ``links.branches``, compare the branch head in git with the source-list entry and append when they differ (the first
    sighting has ``before: null``). When an append voids ``verify`` or ``code`` approvals, the host's
    ``gate.invalidated`` (``new_commits``) follows, so a waiting agent hears of it. Returns the events appended.

    The reader runs **without the workspace lock** (a slow or hostile repository cannot hold every writer up); only
    the append takes it, and the model re-checks ``before`` there (a head that moved meanwhile is refused and looked at
    next time).
    A read-only store, a repo without a working copy, a branch git does not know and a ref that names no commit object
    are skipped; the last is added to ``problems`` (never signed). Call it before ``submit``, ``show`` and ``wait`` (and
    a person's approval prompt, C7). In P1 the source list is only as trustworthy as the working copy the agent can
    write.

    ``backend`` (host mode): the host's reader children, ``look(path, name, ref) -> (sha | None, identity)``
    (:mod:`orch.gitread`); ``None`` (direct mode): the same reader in-process (:data:`orch.gitread.local.READER`, #565).
    Either way no git process runs, and a repository the reader refuses is a problem line, never a guess."""
    from .errors import StoreError

    if not store.can_write:
        return []
    view = store.ticket(ref)
    if view is None or view.status in ("closed",):
        return []
    links = view.fields["links"]
    wanted = []
    for name in links["repos"]:  # the reader, with no lock held
        branch = links["branches"].get(name)
        path = (backend.repo_path if backend is not None else repo_path)(
            store.layout, store.state.workspace.repos, name
        )
        if not branch:
            continue  # the ticket names no branch yet: submit says so itself
        if path is None or not path.is_dir():
            if problems is not None:
                problems.append(f"{name}: no working copy in settings.repos; its code cannot be observed")
            continue
        ref_name = f"refs/heads/{branch}"
        try:
            found, rid = (backend if backend is not None else _LOCAL).look(path, name, ref_name)
        except Unavailable as e:
            if problems is not None:
                problems.append(f"{name}: {e.reason}; its code cannot be observed")
            continue
        if found is None:
            if problems is not None:
                problems.append(f"{name}: git does not know {ref_name}; its code cannot be observed")
            continue
        sha = found
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
