"""Finding the workspace: the one place that opens anything to do so (direct mode).

**Invariant.** The user's starting path (the working directory or ``ORCH_WORKSPACE``) is resolved and trusted; the
walk goes up from the resolved path; anything inside a discovered or candidate workspace (``.orch/``,
``config.json``, ``tickets/``) is opened only through ``O_NOFOLLOW`` directory fds from that root, never by a path.
There is no "check, then open by path" step for a swap to land in: a component swapped for a symlink fails the
``O_NOFOLLOW`` open (``ELOOP``/``ENOTDIR``) instead of being followed.

The start is resolved once and opened once by path: it is where the person stands or what they named, and resolving
it is what keeps ``/tmp`` on macOS or a symlinked home working. The walk then goes up only through ``..`` relative to
the current directory fd (never a symlink), so an ancestor swapped for a symlink after the resolve is never entered;
the paths in a result only name the fds.

What a candidate is:

* **v2**: ``config.json`` with ``"schema": "orch.workspace/2"`` and a ``workspace`` object with a string ``id``,
  in ``<project>/.orch/`` (:mod:`orch.layout`). A config in the project root is a store from an older version: it is
  found (so the person is told to delete it, the open that follows refuses) but never read as a store. Both, or a
  ``.orch`` that is not a real directory, is :class:`~orch.layout.LayoutError`, never a fall-through. ``Found.path``
  is the project; ``Found.store`` the directory holding the config.
* **v1** (the importer's layout, :func:`orch.importer.v1.find_v1`): a ``tickets/`` directory (by ``lstat`` below the
  fd) and ``config.json`` with ``"schema": 1`` (not a bool) and an ``id`` object. Detected, never opened.

Only ``config.json`` is ever read (a regular file, at most 1 MiB); no ticket is read. The bytes read and the root's
identity are returned (:class:`Found`): after discovery nothing reads ``config.json`` by path again, and whatever
opens something else under the root first checks it is still that directory (:func:`same_root`).

**Ownership boundary (orch-52), for the implicit walk only** (no ``ORCH_WORKSPACE``), enforced in :func:`_walk` and
:func:`_level` by ``fstat`` on the exact fds that are then read (never a path lookup between check and read):

* A candidate counts only if its directory (the level itself for an old-version store there, its ``.orch/``, or for
  v1 its ``orchestrator/``) and its ``config.json`` are owned by the current uid (:func:`_owner`) and are not
  group- or other-writable. A workspace another user planted, or one anyone else could rewrite,
  is not a candidate: the walk goes on past it.
* The walk **stops** (no workspace) at the first directory that is sticky or other-writable (``/tmp``,
  ``/private/tmp``, ``/Users/Shared``): it is neither a candidate nor walked past, so a workspace planted in or above
  a shared directory is never used, and it never blocks ``orch setup`` below it. The stop applies to every directory
  the walk visits, the start included: standing in ``/tmp`` itself finds nothing, standing in ``/tmp/x`` (a folder of
  one's own) checks ``/tmp/x`` and stops at ``/tmp``. A workspace directly in a sticky directory is found from a
  working directory inside it, or through ``ORCH_WORKSPACE``.
* **Fail closed**: when the boundary cannot be established (opening a directory or its ``..``, or an ``fstat``,
  fails, e.g. an unreadable ``0o1111`` ancestor), the walk stops with nothing found; it never climbs past it.
* An explicit path (``ORCH_WORKSPACE``) is the person's choice: no ownership rule and no stop, but the same
  ``O_NOFOLLOW`` fd reads inside it.
* Tier B: a group-shared workspace is reached through the host's socket or by an explicit path for the host's uid,
  never by another uid's implicit walk, so there is no group exception to the rule.
"""

from __future__ import annotations

import json
import os
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from orch import layout as _layout
from orch.layout import Layout, LayoutError, StorePin
from orch.privdir import others_can_write

__all__ = ["CONFIG_LIMIT", "CONFIG_SCHEMA", "Found", "LayoutError", "locate", "read_config", "same_root", "v2_config"]

CONFIG_SCHEMA = "orch.workspace/2"
CONFIG_LIMIT = 1 << 20

_DIR = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0)
_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_FILE = os.O_RDONLY | _NOFOLLOW | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_NOCTTY", 0)
_FILE |= getattr(os, "O_CLOEXEC", 0)


@dataclass(frozen=True)
class Found:
    """A workspace found: its ``config.json`` as read from the checked fd (``raw``, and ``config`` parsed from exactly
    those bytes) and the root directory's identity (``ident``: ``st_dev``, ``st_ino`` of the fd the config was read
    below). A caller never reads ``config.json`` again by path; before it opens anything else under ``path`` it checks
    :func:`same_root` (``ident`` ``None``: found through the host ``Files``, which confine every read themselves)."""

    path: Path
    v1: bool
    config: dict[str, Any]
    raw: bytes = b""
    ident: tuple[int, int] | None = None
    #: v2 found by its fds: project and store (``None``: a store from an older version in the root at ``path``, which
    #: the open that follows refuses, or found through host ``Files``)
    layout: Layout | None = None
    #: v2 found by its fds: what the open that follows must find again (``.orch``'s identity; see :class:`StorePin`)
    store_pin: StorePin | None = None

    @property
    def project(self) -> Path:
        """The project root (``path``; registry ``root``). ``ident`` is this directory's identity."""
        return self.path

    @property
    def store(self) -> Path:
        """The directory holding ``config.json``, ``tickets/``, ``.state/``."""
        return self.layout.store if self.layout is not None else self.path / _layout.STORE_DIR


def _open_trusted(path: Path) -> int | None:
    """The resolved start (or one of its ancestors): trusted, opened by path."""
    try:
        return os.open(path, _DIR)
    except OSError:
        return None


def _sub(fd: int, name: str) -> int | None:
    """The directory ``name`` below ``fd``, ``O_NOFOLLOW``: a symlink is ``None``, never followed."""
    try:
        sub = os.open(name, _DIR | _NOFOLLOW, dir_fd=fd)
    except OSError:
        return None
    if not stat.S_ISDIR(os.fstat(sub).st_mode):  # pragma: no cover - O_DIRECTORY already refuses
        os.close(sub)
        return None
    return sub


def _owner() -> int:
    """The uid an implicitly found candidate must belong to (the tests replace this to stand in for another user)."""
    return os.getuid()


def _private(st: os.stat_result) -> bool:
    """Owned by :func:`_owner` and writable by no one else (no group or other write bit)."""
    return st.st_uid == _owner() and not others_can_write(st, _owner())


def _shared(st: os.stat_result) -> bool:
    """A directory the walk stops at: sticky or writable by anyone (``/tmp``, ``/Users/Shared``)."""
    return bool(st.st_mode & (stat.S_ISVTX | stat.S_IWOTH))


def _config_at(fd: int, *, owned: bool = False) -> tuple[dict[str, Any], bytes] | None:
    """``config.json`` below the directory fd, ``O_NOFOLLOW``, a regular file of at most 1 MiB, as a JSON object and
    the bytes it was parsed from. ``owned``: also :func:`_private` (checked on the opened fd), else ``None``."""
    try:
        cfd = os.open("config.json", _FILE, dir_fd=fd)
    except OSError:
        return None
    try:
        st = os.fstat(cfd)
        if not stat.S_ISREG(st.st_mode) or st.st_size > CONFIG_LIMIT or (owned and not _private(st)):
            return None
        os.set_blocking(cfd, True)
        chunks, total = [], 0
        while chunk := os.read(cfd, min(1 << 16, CONFIG_LIMIT + 1 - total)):
            chunks.append(chunk)
            total += len(chunk)
            if total > CONFIG_LIMIT:
                return None
        raw = b"".join(chunks)
        doc = json.loads(raw)
    except (OSError, ValueError, RecursionError):
        return None
    finally:
        os.close(cfd)
    return (doc, raw) if isinstance(doc, dict) else None


def v2_config(doc: dict[str, Any] | None) -> dict[str, Any] | None:
    ws = doc.get("workspace") if isinstance(doc, dict) else None
    if doc is None or doc.get("schema") != CONFIG_SCHEMA or not isinstance(ws, dict):
        return None
    return doc if isinstance(ws.get("id"), str) else None


def _is_v1(fd: int, doc: dict[str, Any] | None) -> bool:
    schema = doc.get("schema") if doc is not None else None
    if schema != 1 or isinstance(schema, bool) or not isinstance(doc.get("id") if doc else None, dict):
        return False
    try:
        st = os.stat("tickets", dir_fd=fd, follow_symlinks=False)
    except OSError:
        return False
    return stat.S_ISDIR(st.st_mode)


def _probe(fd: int, path: Path, *, v2: bool, v1: bool, owned: bool) -> tuple[Found | None, Found | None]:
    """The v2 and the v1 candidate at the directory fd. ``owned`` (the implicit walk): the directory itself and its
    ``config.json`` must be :func:`_private`, else neither is a candidate."""
    st = os.fstat(fd)
    if owned and not _private(st):
        return None, None
    got = _config_at(fd, owned=owned)
    doc, raw = got if got is not None else (None, b"")
    cfg = v2_config(doc) if v2 else None
    ident = (st.st_dev, st.st_ino)
    return (
        Found(path, False, cfg, raw, ident) if cfg is not None else None,
        Found(path, True, doc or {}, raw, ident) if v1 and _is_v1(fd, doc) else None,
    )


def _store_probe(fd: int, d: Path, *, owned: bool, start: bool = True) -> Found | None:
    """The v2 candidate in ``.orch/`` below the project fd (``O_NOFOLLOW``). An entry that is there but not a real
    directory is :class:`LayoutError`, never a fall-through, except in the implicit walk (``owned``) for an entry that
    is not the person's own (or in a project that is not their own): that is no candidate and the walk goes on (a
    stranger's ``.orch`` in a parent directory must not block discovery). ``ident`` is the project's; ``.orch`` is
    pinned by ``store_pin`` (identity, and ``owned``: ownership checked again on the opened fd)."""
    if owned and not _private(os.fstat(fd)):
        return None
    # away from the start, another tool's ``.orch`` (a link, a file) holds no orch config and must not block the walk
    sfd = _layout.open_store_dir(fd, (lambda st: st.st_uid != _owner()) if owned else None, lenient=owned and not start)
    if sfd is None:
        return None
    try:
        got, _ = _probe(sfd, d / _layout.STORE_DIR, v2=True, v1=False, owned=owned)
        sst = os.fstat(sfd)
    finally:
        os.close(sfd)
    if got is None:
        return None
    st = os.fstat(fd)
    pin = StorePin((sst.st_dev, sst.st_ino), owned)
    return Found(d, False, got.config, got.raw, (st.st_dev, st.st_ino), Layout.new_at(d), pin)


def _level(fd: int, d: Path, *, v1_sub: bool, v1: bool, owned: bool, start: bool = True) -> Found | None:
    """The directory open at ``fd`` (named ``d``): v2 first (the old-version root config or ``.orch/``; both is
    :class:`LayoutError`), then v1 (here, else in its ``orchestrator/``, ``O_NOFOLLOW`` from ``fd``). ``owned`` (the
    implicit walk): the candidate rule of the ownership boundary, every check by ``fstat`` on the exact fd that is then
    read (the one place it is enforced, with the stop in :func:`_walk`)."""
    # a ``config.json`` inside a folder named ``.orch`` is the store of the project above it, never an old-version root
    # store: the walk goes on up and finds it there (through :func:`_store_probe`, with every check of that path)
    here2, here1 = _probe(fd, d, v2=d.name != _layout.STORE_DIR, v1=v1, owned=owned)
    # a link/file ``.orch`` next to a legacy config is the double-store case: strict there, as at the start
    new2 = _store_probe(fd, d, owned=owned, start=start or here2 is not None)
    _layout.check_single_store(legacy=here2 is not None, new=new2 is not None)
    if new2 is not None:
        return new2
    if here2 is not None:
        return here2
    sub1 = None
    if v1_sub or v1:
        sfd = _sub(fd, "orchestrator")
        if sfd is not None:
            try:
                _, sub1 = _probe(sfd, d / "orchestrator", v2=False, v1=v1, owned=owned)
            finally:
                os.close(sfd)
    return here1 or sub1


def _walk(here: Path, *, v1: bool) -> Found | None:
    """The implicit walk from the resolved ``here``: opened once by path, then up only through ``..`` relative to the
    current fd (``..`` is never a symlink), so an ancestor swapped for a symlink after the resolve is never entered.
    Each directory's ``fstat`` decides the stop (sticky or other-writable) before anything in it is looked at. Fail
    closed: when the boundary cannot be established (an open or ``fstat`` fails, e.g. an unreadable ``0o1111``
    ancestor), the walk stops with nothing found. ``d`` only names the fd for the result."""
    fd = _open_trusted(here)
    if fd is None:
        return None
    d = here
    try:
        while True:
            try:
                st = os.fstat(fd)
            except OSError:
                return None
            if _shared(st):
                return None
            found = _level(fd, d, v1_sub=True, v1=v1, owned=True, start=d == here)
            if found is not None:
                return found
            try:
                up = os.open("..", _DIR, dir_fd=fd)
            except OSError:
                return None
            try:
                ust = os.fstat(up)
            except OSError:
                os.close(up)
                return None
            os.close(fd)
            fd = up
            if (ust.st_dev, ust.st_ino) == (st.st_dev, st.st_ino) or d.parent == d:  # the root: nothing above
                return None
            d = d.parent
    finally:
        os.close(fd)


def locate(start: str | os.PathLike[str], *, explicit: bool, v1: bool = True) -> Found | None:
    """The nearest workspace from ``start``, which is resolved first (see the module docstring).

    ``explicit`` (``ORCH_WORKSPACE``): only the resolved path itself is a v2 candidate (no walk, no fallback); a v1
    workspace there or in its ``orchestrator/`` is found so the caller can refuse. Otherwise :func:`_walk`: the first
    directory that is (or holds in ``orchestrator/``) a v2 or a v1 workspace wins, v2 first at the same level, under
    the ownership boundary (only the person's own candidates count, the walk stops at a shared directory)."""
    try:
        here = Path(start).resolve()
    except (OSError, RuntimeError):
        return None
    if not explicit:
        return _walk(here, v1=v1)
    if here.name == _layout.STORE_DIR and here.parent != here:  # the alias: ORCH_WORKSPACE=<project>/.orch
        pfd = _open_trusted(here.parent)
        if pfd is not None:
            try:
                alias = _level(pfd, here.parent, v1_sub=False, v1=False, owned=False)
            finally:
                os.close(pfd)
            if alias is not None and alias.layout is not None and alias.layout.store == here:
                return alias  # the project's own .orch confirmed as an orch store; anything else is not an alias
    fd = _open_trusted(here)
    if fd is None:
        return None
    try:
        return _level(fd, here, v1_sub=False, v1=v1, owned=False)
    finally:
        os.close(fd)


def read_config(root: str | os.PathLike[str]) -> dict[str, Any] | None:
    """The v2 ``config.json`` of the workspace whose project is ``root``, or ``None`` (not a workspace, or its layout
    is unusable: a linked ``.orch`` or two stores read as none here; discovery and the store refuse them by name).
    Only ``root`` and what is below it are opened, never its parent (a host serving ``root`` allows nothing outside
    it): ``root`` by path with ``O_NOFOLLOW`` on its last component (the ancestors are the trusted start), then the
    store below that fd, every step ``O_NOFOLLOW``."""
    try:
        fd = os.open(Path(root), _DIR | _NOFOLLOW)
    except OSError:
        return None
    try:
        try:
            found = _layout.resolve(fd, root)
        except LayoutError:  # two stores, a linked .orch, a store from an older version: refused by name elsewhere
            return None
        if found is None:
            return None
        sfd = _layout.open_store_dir(fd)
        if sfd is None:
            return None
        try:
            got = _config_at(sfd)
        finally:
            os.close(sfd)
        return v2_config(got[0] if got is not None else None)
    except LayoutError:
        return None
    finally:
        os.close(fd)


def same_root(root: str | os.PathLike[str], ident: tuple[int, int]) -> bool:
    """``root`` (opened ``O_NOFOLLOW`` on its last component) is still the directory discovery found: the same
    ``st_dev`` and ``st_ino``. A root renamed away, replaced or swapped for a symlink after discovery is ``False``."""
    try:
        fd = os.open(Path(root), _DIR | _NOFOLLOW)
    except OSError:
        return False
    try:
        st = os.fstat(fd)
    except OSError:
        return False
    finally:
        os.close(fd)
    return (st.st_dev, st.st_ino) == tuple(ident)
