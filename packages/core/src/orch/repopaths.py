"""The workspace repos a person declares in ``settings.repos`` (Repos add-on): the host's own checks of a path, and a
read-only look at one.

* :func:`check_declared` runs when an owner PREPARES a ``settings.set`` that names repos (``orch.ops.person_ops``), so a
  path is refused before anything is signed: absolute and plain (no ``..``), an existing folder, a git working copy
  (a real ``.git`` folder or file in it, not a link), not the person's home folder, not the root folder and, in the
  ``.orch`` layout, not the orch store or anything inside it. Resolved with symlinks followed, so a link cannot name
  the store or the home folder from outside. It only CHECKS: nothing is written, git is not run.
  This check runs ONLY when the change is prepared. Nothing re-checks the path when the signed event is applied, and the
  folder can change afterwards: the safeguard at git time is :func:`orch.gitsafe.gate`, which every orch git command
  passes (allow-listed repository config, no program, no submodule), not this module.
* :func:`observe` is the read behind the dashboard's "is it there" column (``workspace.settings``, owners and
  maintainers): ``present`` / ``missing`` / ``not_git`` / ``unreadable`` and the branch git says is checked out. It
  reads ``.git/HEAD`` as a file (no git process, so no hook or config of the repository can run), never follows a
  link, opens nothing that is not a regular file and reads at most 512 bytes.
"""

from __future__ import annotations

import os
import stat
from typing import Any

from orch.layout import Layout

_MAX_HEAD = 512


class RepoPathError(Exception):
    """A declared path orch will not accept; the text is for the person (plain words, one line)."""


def _inside(path: str, root: str) -> bool:
    return path == root or path.startswith(root.rstrip(os.sep) + os.sep)


def _plain(path: Any) -> str:
    if not isinstance(path, str) or not path or len(path) > 1024:
        raise RepoPathError("Write the full path of the repository folder.")
    if any(ord(c) < 32 or ord(c) == 127 for c in path) or "\\" in path:
        raise RepoPathError("The path has characters that cannot be shown safely.")
    if not os.path.isabs(path):
        raise RepoPathError("Write the full path, starting with /.")
    if ".." in path.split("/") or "." in path.split("/") or "//" in path:
        raise RepoPathError("Write the path without '.', '..' or doubled slashes.")
    return path


def _config_dirs() -> list[str]:
    out = [os.path.expanduser("~/.config/orch")]
    xdg = os.environ.get("XDG_CONFIG_HOME", "")
    if xdg.startswith("/"):
        out.append(os.path.join(xdg, "orch"))
    try:  # the product's own answer (a relative ORCH_STATE_DIR is made absolute there)
        from orch.ops.runtime import state_dir

        out.append(os.fspath(state_dir(os.environ)))
    except Exception:  # no HOME and nothing configured: the fixed places above still apply
        pass
    return [os.path.realpath(d) for d in out]


def _not_orch_data(layout: Layout, real: str) -> None:
    """Refuse a (resolved) path that is, or is below, orch's own data: the store, and the user's orch config/state."""
    places = [os.path.realpath(layout.store)]
    if any(_inside(real, p) for p in [*places, *_config_dirs()]):
        raise RepoPathError("A repository cannot be inside orch's own data folder.")


def check_declared(layout: Layout, path: Any) -> None:
    """Raise :class:`RepoPathError` unless ``path`` may be declared as a repo folder (see the module text)."""
    p = _plain(path)
    try:
        real = os.path.realpath(p)
        st = os.stat(real)
    except OSError:
        raise RepoPathError("That folder does not exist on this computer.") from None
    if not stat.S_ISDIR(st.st_mode):
        raise RepoPathError("That path is not a folder.")
    home = os.path.realpath(os.path.expanduser("~"))
    if real in (os.sep, home) or p.rstrip("/") == os.path.expanduser("~").rstrip("/"):
        raise RepoPathError("A repository cannot be your whole home folder (or the root of the disk).")
    _not_orch_data(layout, real)
    try:
        mode = os.lstat(os.path.join(real, ".git")).st_mode
    except OSError:
        raise RepoPathError("That folder is not a git repository (it has no .git).") from None
    if not (stat.S_ISDIR(mode) or stat.S_ISREG(mode)):
        raise RepoPathError("That folder is not a git repository (its .git is not a folder or file).")
    if stat.S_ISREG(mode):  # a linked working copy: where its git data lives must be a plain place too
        try:
            target = _gitdir(real)
        except OSError:
            target = None
        if target is None:
            raise RepoPathError("That folder is not a git repository (its .git file does not name a git folder).")
        _not_orch_data(layout, os.path.realpath(target))


def _read_small(path: str) -> str | None:
    """A small regular file (at most ``_MAX_HEAD`` bytes), never through a link, never blocking; ``None`` otherwise."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | getattr(os, "O_CLOEXEC", 0))
    except OSError:
        return None
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_size > _MAX_HEAD:  # a longer file is never read cut short
            return None
        return os.read(fd, _MAX_HEAD).decode("utf-8", "replace")
    except OSError:
        return None
    finally:
        os.close(fd)


def _gitdir(work: str) -> str | None:
    dot = os.path.join(work, ".git")
    mode = os.lstat(dot).st_mode  # raises FileNotFoundError: caller maps it
    if stat.S_ISDIR(mode):
        return dot
    if stat.S_ISREG(mode):  # a linked working copy: ``gitdir: <path>``
        text = _read_small(dot)
        if text and text.startswith("gitdir:"):
            line = text[7:].strip().split("\n")[0]
            if line and "\0" not in line:
                return os.path.realpath(os.path.join(work, line))  # links resolved first, never `..` against a link
    return None


def observe(layout: Layout, declared_path: str) -> dict[str, Any]:
    """``{"state": present|missing|not_git|unreadable, "branch": str | None}`` for one declared path (joined lexically
    to the project folder as every host-mode read does). Read-only."""
    from orch.gitread.refs import branch_ok as _branch_ok

    work = os.path.normpath(os.path.join(os.fspath(layout.project), declared_path))
    try:
        if not stat.S_ISDIR(os.stat(work).st_mode):
            return {"state": "missing", "branch": None}
    except (FileNotFoundError, NotADirectoryError):
        return {"state": "missing", "branch": None}
    except OSError:
        return {"state": "unreadable", "branch": None}
    try:
        gitdir = _gitdir(work)
    except (FileNotFoundError, NotADirectoryError):
        return {"state": "not_git", "branch": None}
    except OSError:
        return {"state": "unreadable", "branch": None}
    if gitdir is None:
        return {"state": "not_git", "branch": None}
    head = _read_small(os.path.join(gitdir, "HEAD"))
    if head is None:
        return {"state": "not_git", "branch": None}
    line = head.split("\n")[0].strip()
    prefix = "ref: refs/heads/"
    branch = line[len(prefix) :] if line.startswith(prefix) else None
    return {"state": "present", "branch": branch if branch and _branch_ok(branch) else None}


__all__ = ["RepoPathError", "check_declared", "observe"]
