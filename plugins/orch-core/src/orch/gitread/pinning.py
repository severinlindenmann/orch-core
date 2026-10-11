"""Pinning a repository when it first appears (F7): its working copy, gitdir and ``commondir`` by real path, device
and inode.

* The declared ``path`` must be a directory whose ``.git`` is a real directory (the gitdir), or a regular ``.git``
  file ``gitdir: <path>`` (a linked worktree). A ``.git`` symlink, or anything else, is refused.
* The gitdir and the ``commondir`` (from the gitdir's ``commondir`` file, else the gitdir itself) must lie under the
  repo's declared ``path`` or under the ``path`` of another repo declared in ``settings.repos`` (the main checkout of
  a linked worktree). Anything else is refused: a ``.git`` file planted to point at a directory the agent controls
  elsewhere never becomes a pin.

This runs in the child under the ``pin`` profile, whose audit hook allows opens only under the declared paths.
"""

from __future__ import annotations

import os

from . import limits
from .errors import Unavailable
from .fs import Dir, DirPin, RepoPins, open_abs
from .repo import resolve_named

__all__ = ["pin_repo", "under"]


def under(path: str, root: str) -> bool:
    return path == root or path.startswith(root.rstrip("/") + "/")


def _pin_dir(path: str) -> tuple[Dir, DirPin]:
    d, st = open_abs(path)
    return d, DirPin(path, st.st_dev, st.st_ino)


def pin_repo(path: str, roots: list[str]) -> RepoPins:
    """Pins for the working copy at ``path``. ``roots``: the real paths of every repo declared in
    ``settings.repos`` (``path``'s own included)."""
    real = os.path.realpath(path)
    roots = [os.path.realpath(r) for r in roots]
    if real not in roots:
        raise Unavailable("the working copy is not one of the declared repos")
    wt, wt_pin = _pin_dir(real)
    opened = [wt]
    try:
        kind = wt.kind(".git")
        if kind == "dir":
            gitdir_path = real.rstrip("/") + "/.git"
        elif kind == "file":
            data = wt.read(".git", limits.SMALL_FILE_MAX)
            if not data.startswith(b"gitdir: "):
                raise Unavailable("the .git file is not 'gitdir: <path>'")
            gitdir_path = resolve_named(real, data[len(b"gitdir: ") :])
        elif kind is None:
            raise Unavailable("the working copy has no .git")
        else:
            raise Unavailable("the working copy's .git is a symlink or a special file")
        if not any(under(gitdir_path, r) for r in roots):
            raise Unavailable("the gitdir lies outside every declared repo")
        gd, gd_pin = _pin_dir(gitdir_path)
        opened.append(gd)
        if gd.kind("HEAD") != "file":
            raise Unavailable("the gitdir has no HEAD")
        named = gd.read_optional("commondir", limits.SMALL_FILE_MAX)
        if named is None:
            cd_pin = gd_pin
        else:
            cd_path = resolve_named(gitdir_path, named)
            if not any(under(cd_path, r) for r in roots):
                raise Unavailable("the commondir lies outside every declared repo")
            cd, cd_pin = _pin_dir(cd_path)
            opened.append(cd)
            if cd_pin == gd_pin:
                raise Unavailable("a commondir file naming the gitdir itself")
        return RepoPins(wt_pin, gd_pin, cd_pin)
    finally:
        for d in opened:
            d.close()
