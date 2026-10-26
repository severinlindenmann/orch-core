"""Git history for the explorer (issue #491, decision 2), through `orch.gitbrowse`'s closed operations only.

The repository of a path is the nearest folder at or above it, inside the same root, that has its own `.git`. A path
is checked with the guard (plain, not denied) before git sees it; git reads the object database only, so a link in the
working tree is never followed. Every text git returns (names, subjects) is data: made visible, capped.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass

from orch import gitbrowse, gitsafe

from ..errors import fail
from . import describe, guard

PATCH_MAX = 400_000
PAGE_MAX = 200


@dataclass(frozen=True)
class Repo:
    folder: str  # absolute
    prefix: tuple[str, ...]  # parts of the repository folder below the root
    file: str  # the path inside the repository


#: repositories whose layout git confirmed, by folder: for this many seconds
CONFIRM_TTL = 20.0
_confirmed: dict[tuple[int, int], float] = {}


def confirmed(folder: str) -> bool:
    """Does git itself find its git directory, its common directory, its top and its objects at exactly
    ``<folder>/.git``, ``folder`` and ``<folder>/.git/objects``? A repository that git reads from somewhere else (a
    ``.git`` that redirects, a ``commondir``) is not one the guard checked, so it is not used. ``folder`` must
    already be a real path (no link in it). Remembered by the identity of its ``.git`` folder. `HostError`
    (`observe.unavailable`) when the gate refuses the repo."""
    real = os.path.realpath(folder)
    if real != folder:
        return False
    try:
        st = os.stat(os.path.join(real, ".git"), follow_symlinks=False)
    except OSError:
        return False
    key = (st.st_dev, st.st_ino)
    now = time.monotonic()
    if now - _confirmed.get(key, -1e9) < CONFIRM_TTL:
        return True
    try:
        res = gitbrowse.run(folder, "layout", os.environ, max_bytes=8192, timeout=10.0)
    except gitsafe.Unsafe as exc:
        raise fail("observe.unavailable", exc.reason, exit=4) from None
    lines = res.out.decode("utf-8", "surrogateescape").splitlines()
    git_dir = os.path.join(real, ".git")
    if res.code != 0 or lines != [git_dir, git_dir, real, os.path.join(git_dir, "objects")]:
        return False
    _confirmed[key] = now
    return True


def locate(root: guard.Root, parts: tuple[str, ...]) -> Repo | None:
    """The repository that holds ``parts`` (a file or a folder), or None. `Denied` for a locked path."""
    if reason := guard.deny(parts):
        raise guard.Denied(reason)
    for n in range(len(parts) - 1, -1, -1):
        if root.has_git(parts[:n]):
            folder = root.abs(parts[:n])
            if not confirmed(folder):
                return None
            return Repo(folder, parts[:n], "/".join(parts[n:]))
    return None


def _run(repo: Repo, op: str, *operand: str, max_bytes: int = PATCH_MAX, timeout: float = 15.0):
    try:
        return gitbrowse.run(
            repo.folder, op, os.environ, operand=operand, max_bytes=max_bytes,
            timeout=timeout,
        )  # fmt: skip
    except gitsafe.Unsafe as exc:
        raise fail("observe.unavailable", exc.reason, exit=4) from None


def _text(value: str, limit: int = 300) -> str:
    return describe.clean_line(value, limit)


def was_file(repo: Repo) -> bool:
    """Did git ever record ``repo.file`` as a FILE (a deleted file, as a row of its own last change)? A folder, or a
    name that never was a file, has none."""
    if not repo.file:
        return False
    res = _run(repo, "last_change", "HEAD", repo.file, max_bytes=100_000)
    return res.code == 0 and any(path == repo.file for _s, path in gitbrowse.parse_names(res.out))


def _only_the_file(repo: Repo, op: str, *operand: str) -> None:
    """The change touches exactly the requested FILE and nothing else: a folder (at that commit, or a path that was
    one) lists its files, which is refused. An empty change is fine (the commit did not touch the file)."""
    res = _run(repo, op, *operand, repo.file, max_bytes=200_000)
    if res.code != 0 or res.cut:
        raise fail("not_found", "That change is not in this repository.", exit=2)
    if any(path != repo.file for _s, path in gitbrowse.parse_names(res.out)):
        raise fail("not_found", "History is for files. Choose a file.", exit=2)


def _not_a_folder_at(repo: Repo, sha: str) -> None:
    """At ``sha`` the path is a blob, or absent (added or deleted by that change); never a tree or anything else."""
    res = _run(repo, "blob_type", sha, repo.file, max_bytes=64)
    if res.code == 0 and res.out.strip() != b"blob":
        raise fail("not_found", "History is for files. Choose a file.", exit=2)


def log(repo: Repo, skip: int, limit: int) -> dict:
    """The commits that changed the path, newest first: ``{commits, more}``."""
    if not repo.file:
        return {"commits": [], "more": False}
    limit = max(1, min(limit, PAGE_MAX))
    res = _run(repo, "file_log", str(limit + 1), str(skip), "HEAD", repo.file, max_bytes=2_000_000)
    rows = gitbrowse.parse_log(res.out) if res.code == 0 else []
    more = len(rows) > limit or (res.cut and bool(rows))
    return {
        "commits": [
            {"sha": sha, "short": sha[:8], "author": _text(who, 100), "at": when, "subject": _text(subject)}
            for sha, who, when, subject in rows[:limit]
        ],
        "more": more,
    }


def commit(repo: Repo, sha: str) -> dict:
    """One commit's change to the file: the commit and its patch (cut at :data:`PATCH_MAX`)."""
    _not_a_folder_at(repo, sha)
    _only_the_file(repo, "commit_file_names", sha)
    res = _run(repo, "commit_file_patch", "3", sha, repo.file)
    parsed = gitbrowse.parse_show(res.out) if res.code == 0 else None
    if parsed is None:
        raise fail("not_found", "That commit is not in this repository.", exit=2)
    (full, who, when, subject, body), patch = parsed
    return {
        "sha": full,
        "short": full[:8],
        "author": _text(who, 100),
        "at": when,
        "subject": _text(subject),
        "body": _text(body, 4000) if body else "",
        "patch": gitsafe.plain_hunk_headers(patch),
        "truncated": res.cut,
        "empty": not patch.strip(),
    }


def compare(repo: Repo, a: str, b: str) -> dict:
    """The difference of the file between two commits (``a`` older)."""
    _not_a_folder_at(repo, a)
    _not_a_folder_at(repo, b)
    _only_the_file(repo, "range_file_names", a, b)
    res = _run(repo, "file_diff", "3", a, b, repo.file)
    if res.code != 0:
        raise fail("not_found", "One of those versions is not in this repository.", exit=2)
    patch = res.out.decode("utf-8", "replace")
    return {
        "a": a, "b": b, "patch": gitsafe.plain_hunk_headers(patch), "truncated": res.cut, "empty": not patch.strip()
    }  # fmt: skip


def version(repo: Repo, name: str, sha: str) -> dict:
    """The file as it was at commit ``sha``, described like the working copy."""
    kind = _run(repo, "blob_type", sha, repo.file, max_bytes=64)
    if kind.code != 0 or kind.out.strip() != b"blob":
        raise fail("not_found", "The file does not exist in that version.", exit=2)
    size = _run(repo, "blob_size", sha, repo.file, max_bytes=64)
    if size.code != 0 or not size.out.strip().isdigit():
        raise fail("not_found", "The file does not exist in that version.", exit=2)
    n = int(size.out.strip())
    data = None
    if n <= max(describe.TEXT_MAX, describe.IMAGE_MAX):
        got = _run(repo, "blob", sha, repo.file, max_bytes=max(describe.TEXT_MAX, describe.IMAGE_MAX) + 1)
        data = got.out if got.code == 0 and not got.cut else None
    return describe.describe(name, data, n, cut=data is None and n > 0)


def commit_files(repo: Repo, sha: str, before: str | None) -> list[tuple[str, str]]:
    """``(status, path)`` of the files a commit changed (or that changed since ``before``)."""
    res = _run(repo, "range_names", before, sha, max_bytes=500_000) if before else _run(
        repo, "commit_names", sha, max_bytes=500_000
    )  # fmt: skip
    return gitbrowse.parse_names(res.out) if res.code == 0 else []


def on_head(repo: Repo, sha: str) -> bool:
    """Is the commit already on the line the repository's own folder stands on (HEAD)? Asked with the allowed
    operations only: the commit is in HEAD's log of one of the files it changed. Unknown (a commit that changed no
    file, an unreadable answer, a file with a very long log) is False, so the commit is shown rather than hidden."""
    for _status, rel in commit_files(repo, sha, None)[:20]:
        try:
            parts = guard.split(rel)
        except guard.GuardError:
            continue
        if guard.deny(parts):
            continue
        res = _run(repo, "file_log", "500", "0", "HEAD", "/".join(parts), max_bytes=2_000_000)
        if res.code != 0:
            return False
        return any(row[0] == sha for row in gitbrowse.parse_log(res.out))
    return False


def search(repo: Repo, query: str) -> list[tuple[str, int, str]]:
    res = _run(repo, "grep", query, max_bytes=1_000_000, timeout=10.0)
    return gitbrowse.parse_grep(res.out) if res.code == 0 else []


__all__ = ["Repo", "locate", "log", "commit", "compare", "version", "commit_files", "on_head", "search"]
