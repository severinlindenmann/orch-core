"""Where a workspace's files live: the one structural helper for the ``.orch/`` store move (design B11-P2).

A workspace has a **project** (the root: registry ``root``, the agents' and terminals' working directory, the base of
relative ``settings.repos`` paths) and a **store** (the directory holding ``config.json``, ``keys.jsonl``,
``AGENTS.orch.md``, ``events/``, ``tickets/``, ``addons/`` and ``.state/``). In the legacy layout they are the same
directory; in the new layout the store is ``<project>/.orch``.

Nobody joins ``".orch"`` or a store name by hand: code that needs the store asks the :class:`Layout`. A CI test
(``tests/test_layout_lint.py``) greps for such joins outside this module and ``orch/store/``; the files that still do
are listed with exact counts in ``tests/layout_lint_allowlist.txt`` and the list only shrinks.

``WRITE_NEW`` is the flip switch: while it is ``False`` nothing creates a ``.orch`` store; readers understand both
layouts. The flip PR sets it ``True``.
"""

from __future__ import annotations

import json
import os
import stat
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

__all__ = [
    "CONFIG_SCHEMA",
    "STORE_DIR",
    "WRITE_NEW",
    "Layout",
    "LayoutError",
    "StorePin",
    "check_single_store",
    "fresh",
    "open_store_dir",
    "resolve",
]

STORE_DIR = ".orch"
CONFIG_SCHEMA = "orch.workspace/2"
_CONFIG_LIMIT = 1 << 20

#: The flip switch (design §6): while ``False`` no code creates a ``.orch`` store (a new workspace is made in the
#: legacy layout); readers understand both layouts either way. The flip PR sets it ``True``.
WRITE_NEW = False

_DIR = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
_FILE = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_CLOEXEC", 0)


class LayoutError(Exception):
    """The layout is not usable (two stores, a linked ``.orch``). Callers map it to ``validation.path``. ``hint``: what
    the person can do about this particular refusal."""

    def __init__(self, message: str, hint: str | None = None) -> None:
        super().__init__(message)
        self.hint = hint


@dataclass(frozen=True)
class Layout:
    """``project`` (the root) and ``store`` (where the store files are); ``legacy``: the store is the project itself."""

    project: Path
    store: Path
    legacy: bool

    @classmethod
    def legacy_at(cls, project: str | os.PathLike[str]) -> Layout:
        p = Path(project)
        return cls(p, p, True)

    @classmethod
    def new_at(cls, project: str | os.PathLike[str]) -> Layout:
        p = Path(project)
        return cls(p, p / STORE_DIR, False)


@dataclass(frozen=True)
class StorePin:
    """What discovery saw of the store, for the open that follows: ``ident`` the ``(st_dev, st_ino)`` of ``.orch``
    (``None``: the legacy layout, the store is the project), ``owned``: the walk was implicit, so the opened store
    directory must again be the current user's and not group/other-writable (checked on the opened fd)."""

    ident: tuple[int, int] | None
    owned: bool = False


def is_private(st: os.stat_result) -> bool:
    """Owned by the current user and writable by no one else."""
    return st.st_uid == os.getuid() and not st.st_mode & (stat.S_IWGRP | stat.S_IWOTH)


def check_single_store(*, legacy: bool, new: bool) -> None:
    """An orch config in the project root AND in ``.orch/`` is refused (design §5)."""
    if legacy and new:
        raise LayoutError(
            "two orch stores in one project (config.json in the root and in .orch/); "
            "finish `orch migrate` or remove one",
            hint="run `orch migrate`, or remove one of the two stores",
        )


def fresh(project: str | os.PathLike[str]) -> Layout:
    """The layout of a project that holds no store yet (a workspace being created): the switch decides."""
    return Layout.new_at(project) if WRITE_NEW else Layout.legacy_at(project)


def _orch_config(fd: int) -> bool:
    """``config.json`` below the directory fd is a regular file holding an orch v2 config (``O_NOFOLLOW``)."""
    try:
        cfd = os.open("config.json", _FILE, dir_fd=fd)
    except OSError:
        return False
    try:
        st = os.fstat(cfd)
        if not stat.S_ISREG(st.st_mode) or st.st_size > _CONFIG_LIMIT:
            return False
        os.set_blocking(cfd, True)
        doc: Any = json.loads(os.read(cfd, _CONFIG_LIMIT + 1))
    except (OSError, ValueError, RecursionError):
        return False
    finally:
        os.close(cfd)
    return isinstance(doc, dict) and doc.get("schema") == CONFIG_SCHEMA


def resolve(project_fd: int, project: str | os.PathLike[str]) -> Layout | None:
    """The layout of the project open at ``project_fd``, or ``None`` when it holds no orch store.

    Everything is looked at below the fd with ``O_NOFOLLOW`` (a symlinked ``.orch`` is not a store: ``None``). Both an
    orch config in the root and one in ``.orch/`` is :class:`LayoutError` (:func:`check_single_store`)."""
    legacy = _orch_config(project_fd)
    new = False
    sfd = open_store_dir(project_fd)
    if sfd is not None:
        try:
            new = _orch_config(sfd)
        finally:
            os.close(sfd)
    check_single_store(legacy=legacy, new=new)
    if new:
        return Layout.new_at(project)
    return Layout.legacy_at(project) if legacy else None


def open_store_dir(
    project_fd: int, skip: Callable[[os.stat_result], bool] | None = None, *, lenient: bool = False
) -> int | None:
    """The fd of ``.orch`` below the project fd (``O_NOFOLLOW``), or ``None`` when there is no ``.orch``. Anything that
    is there but not a real directory (a symlink, a file) is :class:`LayoutError`: it never falls through to the legacy
    layout (design §5). ``skip(lstat)`` true: not looked at, ``None`` (the implicit walk ignores what is not the
    person's own). ``lenient``: a non-directory is ``None`` too (it cannot hold a config; the implicit walk, away
    from its start, passes another tool's ``.orch``)."""
    try:
        st = os.stat(STORE_DIR, dir_fd=project_fd, follow_symlinks=False)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise LayoutError(f"{STORE_DIR} cannot be examined ({exc.strerror})") from None
    if skip is not None and skip(st):
        return None
    if not stat.S_ISDIR(st.st_mode):
        if lenient:  # not a directory, so it holds no orch config: the caller walks on
            return None
        raise LayoutError(
            f"{STORE_DIR} is not a real directory (a link or a file); refusing to use this project",
            hint=f"rename or remove {STORE_DIR}: orch keeps its store in a real {STORE_DIR}/ directory",
        )
    try:
        return os.open(STORE_DIR, _DIR, dir_fd=project_fd)
    except OSError as exc:
        raise LayoutError(f"{STORE_DIR} cannot be opened ({exc.strerror})") from None
