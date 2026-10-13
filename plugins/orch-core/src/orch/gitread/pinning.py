"""Pinning a repository when it first appears (F7): its working copy, gitdir and ``commondir`` by real path, device
and inode.

* Every directory is opened by walking its real path from ``/`` with ``O_NOFOLLOW`` on each component (or relative
  to an fd already opened that way), and the pin is the ``fstat`` of that fd: a component swapped for a symlink
  between the check and the open is refused, never followed.
* The declared ``path`` must be a directory whose ``.git`` is a real directory with no ``commondir`` file (a main
  repository), or a regular ``.git`` file naming ``<C>/worktrees/<id>`` of another declared repo's ``.git`` ``<C>``,
  with that worktree's ``commondir`` naming ``<C>`` and its ``gitdir`` back-pointer naming this ``.git`` (git's
  layout, exactly; see :func:`pin_repo`). Anything else is refused: a ``.git`` file or ``commondir`` file planted to
  borrow another repo's refs and objects never becomes a pin.

This runs in the child under the ``pin`` profile, whose audit hook allows opens only under the declared paths.
"""

from __future__ import annotations

import os

from . import limits
from .errors import Unavailable
from .fs import Dir, DirPin, RepoPins, abs_parts, open_walk
from .repo import resolve_named

__all__ = ["pin_repo", "under", "verify_worktree_pins"]


def under(path: str, root: str) -> bool:
    """Component-wise, byte for byte (no case folding, no Unicode normalisation)."""
    p, r = abs_parts(path), abs_parts(root)
    return p[: len(r)] == r


def _pin_dir(path: str) -> tuple[Dir, DirPin]:
    d, st = open_walk(path)  # no symlink anywhere along the path: the fd is the directory at this real path
    return d, DirPin(path, st.st_dev, st.st_ino)


def _pin_fd(d: Dir) -> DirPin:
    st = os.fstat(d.fd)
    return DirPin(d.path, st.st_dev, st.st_ino)


REAL_PATH_HINT = "declare the real path of the repo (its realpath, no symlinked component) in settings.repos"


def pin_repo(path: str, roots: list[str] | None) -> RepoPins:
    """Pins for the working copy at ``path``. ``roots``: the paths of every repo declared in ``settings.repos``
    (``path``'s own included), as the host computed them once from the owner-signed settings. ``None`` (direct mode
    only: no registration, the CLI's own environment) skips the containment checks but keeps the layout checks.

    **One interpretation of a path:** every path here is taken byte for byte and must be clean (absolute, no ``.``,
    ``..``, empty component or trailing slash); it is never resolved, case-folded or Unicode-normalised. It is opened
    by the ``O_NOFOLLOW`` walk, so a path with a symlinked component (``/var`` on macOS, or one an agent planted) is
    refused: declare repos by their real paths. The pin is the ``fstat`` of the fd that walk produced.

    **git's layout, exactly (review finding 1):**

    * a ``.git`` **directory** is a main repository: it must have no ``commondir`` file (only
      ``<common>/worktrees/<id>`` has one), so its commondir is itself;
    * a ``.git`` **file** is a linked worktree: it must name ``<C>/worktrees/<id>`` where ``<C>`` is the ``.git``
      directory of another declared repo, that gitdir's ``commondir`` must name exactly ``<C>``, and its back-pointer
      ``gitdir`` file must name this working copy's ``.git``, byte for byte. Anything else (a submodule's
      ``.git/modules/<name>``, a ``.git`` file naming another repo's ``.git`` itself) is refused."""
    real = path
    abs_parts(real)
    for r in roots or ():
        abs_parts(r)
    if roots is not None and real not in roots:
        raise Unavailable("the working copy is not one of the declared repos")
    try:
        wt, wt_pin = _pin_dir(real)
    except Unavailable as e:
        raise Unavailable(f"{e.reason}; {REAL_PATH_HINT}") from None
    opened = [wt]
    try:
        kind = wt.kind(".git")
        if kind == "dir":
            gd = wt.sub(".git")  # relative to the working copy's fd, O_NOFOLLOW: a swapped .git is refused
            opened.append(gd)
            if gd.kind("commondir") is not None:
                raise Unavailable("a .git directory with a commondir file (only a linked worktree's gitdir has one)")
            if gd.kind("HEAD") != "file":
                raise Unavailable("the gitdir has no HEAD")
            gd_pin = _pin_fd(gd)
            return RepoPins(wt_pin, gd_pin, gd_pin)
        if kind is None:
            raise Unavailable("the working copy has no .git")
        if kind != "file":
            raise Unavailable("the working copy's .git is a symlink or a special file")
        data = wt.read(".git", limits.SMALL_FILE_MAX)
        if not data.startswith(b"gitdir: "):
            raise Unavailable("the .git file is not 'gitdir: <path>'")
        gitdir_path = resolve_named(real, data[len(b"gitdir: ") :])
        parts = abs_parts(gitdir_path)
        if len(parts) < 3 or parts[-2] != "worktrees":
            raise Unavailable("a .git file must name <repo>/.git/worktrees/<id>")
        common_path = "/" + "/".join(parts[:-2])
        if roots is not None and not any(common_path == r.rstrip("/") + "/.git" for r in roots if r != real):
            raise Unavailable("the .git file names a worktree of no other declared repo (outside every declared repo)")
        cd, cd_pin = _pin_dir(common_path)
        opened.append(cd)
        if cd.kind("commondir") is not None or cd.kind("HEAD") != "file":
            raise Unavailable("the commondir is not a main repository's .git")
        gd = cd.sub("worktrees/" + parts[-1])  # relative to the pinned commondir fd
        opened.append(gd)
        gd_pin = _pin_fd(gd)  # its path is <C>/worktrees/<id>, the path the .git file names
        if gd.kind("HEAD") != "file":
            raise Unavailable("the gitdir has no HEAD")
        named = gd.read_optional("commondir", limits.SMALL_FILE_MAX)
        if named is None or resolve_named(gitdir_path, named) != common_path:
            raise Unavailable("the worktree's commondir does not name its <repo>/.git")
        back = gd.read_optional("gitdir", limits.SMALL_FILE_MAX)
        if back is None or back.rstrip(b"\n") != (real.rstrip("/") + "/.git").encode():
            raise Unavailable("the worktree's gitdir back-pointer does not name this working copy")
        return RepoPins(wt_pin, gd_pin, cd_pin)
    finally:
        for d in opened:
            d.close()


def verify_worktree_pins(path: str, pins: RepoPins) -> None:
    """In process (direct mode, no reader child): the registered worktree at ``path`` is still the pinned one. It is
    pinned again from the commondir's root and every pin (working copy, gitdir, commondir: device, inode, gitdir path)
    must equal the registered one; anything else, including a ``.git`` file re-pointed elsewhere, is
    :class:`Unavailable`."""
    common = pins.commondir.path
    if not common.endswith("/.git") or len(common) <= len("/.git"):
        raise Unavailable("the worktree's commondir is not <repo>/.git")
    now = pin_repo(path, [path, common[: -len("/.git")]])
    same = (
        (now.worktree.dev, now.worktree.ino) == (pins.worktree.dev, pins.worktree.ino)
        and (now.gitdir.dev, now.gitdir.ino, now.gitdir.path) == (pins.gitdir.dev, pins.gitdir.ino, pins.gitdir.path)
        and (now.commondir.dev, now.commondir.ino) == (pins.commondir.dev, pins.commondir.ino)
    )
    if not same:
        raise Unavailable("the worktree's .git file or location changed since it was registered")
