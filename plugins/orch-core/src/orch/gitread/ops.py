"""The requests the reader answers, the same in the child (host mode) and in-process (direct mode).

* ``pin {path, roots}`` (profile ``pin``): the pins of a working copy (:mod:`orch.gitread.pinning`).
* ``observe {refs}`` (profile ``gitdir``): ``HEAD``'s branch, the commit each ref names, the raw
  ``remote.origin.url`` values.
* ``diff {branch, default_branch, context, max_bytes}`` (profile ``gitdir``): base and head per section 0.8 and the
  patch text of ``git diff --no-ext-diff --no-textconv --no-renames base head``.

Every request first opens the repository under its pins and reads all its refs, so a config, extension or ref store
the reader refuses makes **every** request unavailable, not just the ones that would touch the bad part.
"""

from __future__ import annotations

import base64
from typing import Any

from . import limits
from .errors import Unavailable
from .fs import RepoPins
from .patch import diff_trees, render
from .pinning import pin_repo
from .repo import Repo

__all__ = ["PROFILES", "allowed_paths", "run"]

PROFILES = ("gitdir", "worktree", "pin")
_OPS = {"pin": ("pin",), "observe": ("gitdir", "worktree"), "diff": ("gitdir", "worktree")}


def _str(args: dict[str, Any], key: str, *, optional: bool = False) -> str | None:
    v = args.get(key)
    if v is None and optional:
        return None
    if not isinstance(v, str) or not v or len(v) > 4096 or "\0" in v:
        raise Unavailable(f"bad request: {key}")
    return v


def allowed_paths(profile: str, hello: dict[str, Any]) -> list[str]:
    """The directories the profile may open: the pins (``gitdir``: gitdir and ``commondir``; ``worktree``: those and
    the working copy) or, for ``pin``, the declared repo paths."""
    if profile == "pin":
        roots = hello.get("roots")
        if not isinstance(roots, list) or not roots or not all(isinstance(r, str) and r.startswith("/") for r in roots):
            raise Unavailable("bad request: roots")
        return list(roots)
    pins = RepoPins.from_json(hello.get("pins"))
    out = [pins.gitdir.path, pins.commondir.path]
    if profile == "worktree":
        out.append(pins.worktree.path)
    return out


def _branch_ref(name: str) -> str:
    return name if name.startswith("refs/heads/") else "refs/heads/" + name


def observe(repo: Repo, refs: list[str]) -> dict[str, Any]:
    resolved = {r: repo.resolve_commit(r) for r in refs}  # reads the whole ref store first
    try:
        head_ref: str | None = repo.head_ref()
        head_error = None
    except Unavailable as e:
        head_ref, head_error = None, e.reason
    head_sha = repo.resolve_commit(head_ref) if head_ref else None
    return {
        "head_ref": head_ref,
        "head_sha": head_sha,
        "head_error": head_error,
        "refs": resolved,
        "origin_urls": repo.config.origin_urls,
    }


def diff(repo: Repo, branch: str, default_branch: str | None, context: int, max_bytes: int) -> dict[str, Any]:
    repo.refs  # noqa: B018 (the ref store is checked as a whole first)
    head = repo.resolve_commit(_branch_ref(branch))
    if head is None:
        raise Unavailable(f"the branch {branch!r} does not exist")
    base = None
    for name in [default_branch] if default_branch else ["main", "master"]:
        tip = repo.resolve_commit(_branch_ref(name))
        if tip is not None:
            base = repo.merge_base(head, tip)
            break
    if base is None:
        parents = repo.commit(head).parents
        if not parents:
            raise Unavailable("no merge base with the default branch and no parent commit")
        base = parents[0]
    changes, files_cut = diff_trees(repo, repo.commit(base).tree, repo.commit(head).tree, limits.DIFF_FILES_MAX)
    patch, files, text_cut = render(repo, changes, context, max_bytes)
    cut = patch
    if len(patch) > max_bytes:
        nl = patch.rfind(b"\n", 0, max_bytes)
        cut = patch[: nl + 1] if nl >= 0 else b""
    return {
        "branch": branch,
        "base_sha": base,
        "head_sha": head,
        "files": files,
        "patch": base64.b64encode(cut).decode("ascii"),
        "truncated": len(cut) < len(patch) or files_cut or text_cut,
        "origin_urls": repo.config.origin_urls,
    }


def run(profile: str, hello: dict[str, Any], req: dict[str, Any]) -> dict[str, Any]:
    op = req.get("op")
    args = req.get("args") or {}
    if op not in _OPS or profile not in _OPS[op] or not isinstance(args, dict):
        raise Unavailable(f"bad request: {op!r} under profile {profile!r}")
    if op == "pin":
        path = _str(args, "path")
        assert path is not None
        return {"pins": pin_repo(path, list(hello["roots"])).to_json()}
    with Repo(RepoPins.from_json(hello.get("pins"))) as repo:
        if op == "observe":
            refs = args.get("refs") or []
            if not isinstance(refs, list) or len(refs) > 64 or not all(isinstance(r, str) for r in refs):
                raise Unavailable("bad request: refs")
            return observe(repo, refs)
        context = args.get("context", 3)
        max_bytes = args.get("max_bytes", 262144)
        if type(context) is not int or not 0 <= context <= 10:
            raise Unavailable("bad request: context")
        if type(max_bytes) is not int or not 1024 <= max_bytes <= 1048576:
            raise Unavailable("bad request: max_bytes")
        branch = _str(args, "branch")
        assert branch is not None
        return diff(repo, branch, _str(args, "default_branch", optional=True), context, max_bytes)
