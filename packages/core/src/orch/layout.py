"""Where a workspace's files live: the one structural helper for the ``.orch/`` store move (design B11-P2).

A workspace has a **project** (the root: registry ``root``, the agents' and terminals' working directory, the base of
relative ``settings.repos`` paths) and a **store** (the directory holding ``config.json``, ``keys.jsonl``,
``AGENTS.orch.md``, ``events/``, ``tickets/``, ``addons/`` and ``.state/``): ``<project>/.orch``. A store from an older
version sits directly in the project root; it is detected (:func:`resolve` raises :class:`LegacyStoreError`) and
refused with a message that says to delete it: there is no migration and no support for it.

Nobody joins ``".orch"`` or a store name by hand: code that needs the store asks the :class:`Layout`. A CI test
(``tests/test_layout_lint.py``) greps for such joins outside this module and ``orch/store/``; the files that still do
are listed with exact counts in ``tests/layout_lint_allowlist.txt`` and the list only shrinks.
"""

from __future__ import annotations

import errno
import json
import os
import re
import stat
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from orch.privdir import others_can_write

__all__ = [
    "CONFIG_SCHEMA",
    "KEYS_FILE",
    "STORE_DIR",
    "LegacyStoreError",
    "Layout",
    "LayoutError",
    "StorePin",
    "WORK_DIR",
    "check_single_store",
    "delete_workdir",
    "ensure_workdir",
    "workdir_state",
    "workdir_stat",
    "workdir_summary",
    "workdir_files",
    "workdir_read",
    "WorkdirTooBig",
    "fresh",
    "open_state_fd",
    "state_marker_present",
    "open_store_dir",
    "store_of",
    "resolve",
    "legacy_refusal_at",
    "STATE_DIR",
]

STORE_DIR = ".orch"
#: the file of a store that holds its keys (named here so a deny list elsewhere does not spell it)
KEYS_FILE = "keys.jsonl"
#: per-ticket work folders of tickets without a codebase: ``<project>/.orch-work/<KEY>/`` (beside the store)
WORK_DIR = ".orch-work"
_WORK_KEY = re.compile(r"[A-Z][A-Z0-9]{0,15}-(?:(?!0000)[0-9]{4}|[1-9][0-9]{4,})")
#: orch's local state folder (never committed, never shown): ``<store>/.state``
STATE_DIR = ".state"
CONFIG_SCHEMA = "orch.workspace/2"
_CONFIG_LIMIT = 1 << 20

_DIR = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
_FILE = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_CLOEXEC", 0)


class LayoutError(Exception):
    """The layout is not usable (two stores, a linked ``.orch``). Callers map it to ``validation.path``. ``hint``: what
    the person can do about this particular refusal."""

    def __init__(self, message: str, hint: str | None = None) -> None:
        super().__init__(message)
        self.hint = hint


class LegacyStoreError(LayoutError):
    """The project holds a store from an older version in its root (not in ``.orch/``): refused, with the message that
    says to delete it. ``str(exc)`` is :func:`legacy_refusal`."""


@dataclass(frozen=True)
class Layout:
    """``project`` (the root) and ``store`` (where the store files are: ``<project>/.orch``)."""

    project: Path
    store: Path

    @classmethod
    def new_at(cls, project: str | os.PathLike[str]) -> Layout:
        p = Path(project)
        return cls(p, p / STORE_DIR)

    def workdir(self, key: str) -> Path:
        """``<project>/.orch-work/<KEY>``: the one constructor of a ticket's work folder. ``key`` must be a ticket key
        (so it can never be ``..`` or hold a separator); anything else is :class:`LayoutError`."""
        if not isinstance(key, str) or not _WORK_KEY.fullmatch(key):
            raise LayoutError("not a ticket key")
        return self.project / WORK_DIR / key

    def artifact_rel(self, uid: str, name: str) -> Path:
        """Where the file of artifact ``name`` of ticket ``uid`` lies, relative to the project."""
        return (self.store / "tickets" / uid / "artifacts" / name).relative_to(self.project)


@dataclass(frozen=True)
class StorePin:
    """What discovery saw of the store, for the open that follows: ``ident`` the ``(st_dev, st_ino)`` of ``.orch``,
    ``owned``: the walk was implicit, so the opened store
    directory must again be the current user's and not group/other-writable (checked on the opened fd)."""

    ident: tuple[int, int]
    owned: bool = False


_LEGACY_NAMES = ("config.json", "keys.jsonl", "AGENTS.orch.md", "events", "tickets", ".state", "addons")


def _has_below(project_fd: int, name: str, leaf: str, *, one_deeper: bool) -> bool:
    """``name/leaf`` (or ``name/<any>/leaf``) exists below the project fd, a real entry (no link followed)."""
    try:
        dfd = os.open(name, _DIR, dir_fd=project_fd)
    except OSError:
        return False
    try:
        subs = [None] if not one_deeper else []
        if one_deeper:
            try:
                subs = [n for n in os.listdir(dfd)[:200]]
            except OSError:
                return False
        for sub in subs:
            try:
                if sub is None:
                    os.stat(leaf, dir_fd=dfd, follow_symlinks=False)
                else:
                    os.stat(os.path.join(sub, leaf), dir_fd=dfd, follow_symlinks=False)
                return True
            except OSError:
                continue
        return False
    finally:
        os.close(dfd)


def legacy_refusal(project_fd: int) -> str:
    """The one plain message for a store from an older version in the project root: orch now keeps its files in
    ``.orch/``, and there is no migration (start fresh). Names only what is recognisably orch's below the project fd
    (``config.json`` by its schema, which the caller found; a folder only by a file orch writes in it): a project's
    own ``events/`` or ``tickets/`` is never listed for deletion."""
    sure = ["config.json"]
    unsure = []
    for name in _LEGACY_NAMES[1:]:
        try:
            st = os.stat(name, dir_fd=project_fd, follow_symlinks=False)
        except OSError:
            continue
        if name == "keys.jsonl" or name == "AGENTS.orch.md":
            ours = stat.S_ISREG(st.st_mode)
        elif name == "events":
            ours = _has_below(project_fd, name, "workspace.jsonl", one_deeper=False)
        elif name == "tickets":
            ours = _has_below(project_fd, name, "events.jsonl", one_deeper=True)
        elif name == ".state":
            ours = _has_below(project_fd, name, "lock", one_deeper=False)
        else:  # addons
            ours = _has_below(project_fd, name, "orch-addon.json", one_deeper=True)
        (sure if ours else unsure).append(name + ("" if stat.S_ISREG(st.st_mode) else "/"))
    text = (
        "this folder has an orch store from an older version in the project root; orch now keeps its files in .orch/. "
        f"Delete {', '.join(sure)} and run orch setup again"
    )
    if unsure:
        text += f" (check {', '.join(unsure)} before deleting: they may be your own)"
    return text


def legacy_refusal_at(project: str | os.PathLike[str]) -> str:
    """:func:`legacy_refusal` for the project folder at ``project`` (opened here, ``O_NOFOLLOW`` on the last part)."""
    fd = os.open(project, _DIR)
    try:
        return legacy_refusal(fd)
    finally:
        os.close(fd)


def is_private(st: os.stat_result) -> bool:
    """Owned by the current user and writable by no one else."""
    return st.st_uid == os.getuid() and not others_can_write(st, os.getuid())


def check_single_store(*, legacy: bool, new: bool) -> None:
    """An orch config in the project root AND in ``.orch/`` is refused (design §5)."""
    if legacy and new:
        raise LayoutError(
            "two orch stores in one project (an orch config.json in the project root and one in .orch/); "
            "delete the one in the project root (config.json) and run orch setup again",
            hint="delete config.json in the project root, then run orch setup again",
        )


def fresh(project: str | os.PathLike[str]) -> Layout:
    """The layout of a project that holds no store yet (a workspace being created)."""
    return Layout.new_at(project)


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
    orch config in the root and one in ``.orch/`` is :class:`LayoutError` (:func:`check_single_store`); one in the root
    alone is :class:`LegacyStoreError` (a store from an older version: refused, never read)."""
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
    if legacy:
        raise LegacyStoreError(legacy_refusal(project_fd))
    return None


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


def open_state_fd(project: str | os.PathLike[str]) -> int | None:
    """The fd of ``<store>/.state`` of the project, every step ``O_NOFOLLOW`` from the project fd;
    ``None`` when there is no store or no ``.state``. :class:`LayoutError` when the layout is unusable (two stores, a
    linked ``.orch``, a store from an older version)."""
    try:
        pfd = os.open(project, _DIR)
    except OSError:
        return None
    try:
        found = resolve(pfd, project)
        if found is None:
            return None
        sfd = open_store_dir(pfd)
        if sfd is None:
            return None
        try:
            return os.open(".state", _DIR, dir_fd=sfd)
        except OSError:
            return None
        finally:
            os.close(sfd)
    finally:
        os.close(pfd)


def store_of(project: str | os.PathLike[str]) -> Path:
    """The store directory of the project as it is on disk now (``.orch``, also when there is no store yet).
    Unusable layout: :class:`LayoutError`. A path, for reading names that carry no authority; anything that must be
    pinned goes through an fd (:func:`open_state_fd`, ``Store.open``)."""
    try:
        pfd = os.open(project, _DIR)
    except OSError:
        return Path(project)
    try:
        found = resolve(pfd, project)
    finally:
        os.close(pfd)
    return found.store if found is not None else fresh(project).store


def state_marker_present(project: str | os.PathLike[str], name: str) -> bool:
    """A marker file ``name`` in the ``.state`` of the project's store, in any form. Fails closed: ``False`` only when
    its absence is shown positively (``ENOENT`` on a component or on the file) in BOTH candidate places,
    ``<project>/.state`` and ``<project>/.orch/.state``, each walked without following a link. Any other answer (a
    link, a file where a directory belongs, a permission error, two stores) counts as present. A project that does not
    exist has no marker."""
    try:
        pfd = os.open(project, _DIR)
    except FileNotFoundError:
        return False
    except (OSError, ValueError):
        return True
    try:
        try:
            resolve(pfd, project)
        except LayoutError:
            return True
        return any(_marked_below(pfd, chain, name) for chain in ((".state",), (STORE_DIR, ".state")))
    finally:
        os.close(pfd)


def _marked_below(pfd: int, chain: tuple[str, ...], name: str) -> bool:
    fds: list[int] = []
    fd = pfd
    try:
        for part in chain:
            try:
                fd = os.open(part, _DIR, dir_fd=fd)
            except FileNotFoundError:
                return False
            except OSError:
                return True
            fds.append(fd)
        try:
            os.stat(name, dir_fd=fd, follow_symlinks=False)
        except FileNotFoundError:
            return False
        except OSError:
            return True
        return True
    finally:
        for item in fds:
            os.close(item)


# -- ticket work folders (tickets without a codebase; design r3 part C) -------------------------------------------


def _walk_workdir(layout: Layout, key: str, *, create: bool) -> tuple[int, str] | None:
    """Open ``<project>/.orch-work/<KEY>`` one component at a time below the project fd with ``O_NOFOLLOW`` (a symlinked
    ``.orch-work`` or ``<KEY>``, or a non-directory or foreign-owned one, is :class:`LayoutError`). Returns the fd of
    the folder, or ``None`` when it is absent and ``create`` is false. ``create``: made 0700 where missing."""
    layout.workdir(key)  # the key check
    try:
        pfd = os.open(layout.project, _DIR)
    except OSError as exc:
        raise LayoutError(f"the project folder cannot be opened ({exc.strerror})") from None
    fd = pfd
    try:
        for part in (WORK_DIR, key):
            try:
                st = os.stat(part, dir_fd=fd, follow_symlinks=False)
            except FileNotFoundError:
                if not create:
                    return None
                try:
                    os.mkdir(part, 0o700, dir_fd=fd)
                except FileExistsError:
                    pass
                except OSError as exc:
                    raise LayoutError(f"{part} cannot be created ({exc.strerror})") from None
                st = os.stat(part, dir_fd=fd, follow_symlinks=False)
            except OSError as exc:
                raise LayoutError(f"{part} cannot be examined ({exc.strerror})") from None
            if not stat.S_ISDIR(st.st_mode):
                raise LayoutError(f"{part} is not a real directory (a link or a file); refusing to use it")
            try:
                nxt = os.open(part, _DIR, dir_fd=fd)
            except OSError as exc:
                raise LayoutError(f"{part} cannot be opened ({exc.strerror})") from None
            if fd != pfd:
                os.close(fd)
            fd = nxt
            ost = os.fstat(fd)
            if (ost.st_dev, ost.st_ino) != (st.st_dev, st.st_ino) or ost.st_uid != os.getuid():
                raise LayoutError(f"{part} changed or is not yours; refusing to use it")
        out = fd
        fd = -1
        return out, key
    finally:
        if fd not in (-1, pfd):
            os.close(fd)
        os.close(pfd)


def workdir_stat(layout: Layout, key: str) -> os.stat_result | None:
    """The stat of the ticket's work folder, or ``None`` when it does not exist yet. :class:`LayoutError` for a link, a
    file, a foreign owner. Reads only; a plan uses it, the start creates (:func:`ensure_workdir`)."""
    got = _walk_workdir(layout, key, create=False)
    if got is None:
        return None
    try:
        return os.fstat(got[0])
    finally:
        os.close(got[0])


def ensure_workdir(layout: Layout, key: str) -> os.stat_result:
    """Create the work folder (and ``.orch-work/`` with a ``.gitignore`` of ``*`` so no repository above sees it) where
    missing, 0700, every step without following a link, and return its stat for the cwd pin."""
    got = _walk_workdir(layout, key, create=True)
    assert got is not None
    try:
        parent = os.open("..", _DIR, dir_fd=got[0])
        try:
            gi = os.open(
                ".gitignore", os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600, dir_fd=parent
            )
        except FileExistsError:
            pass
        except OSError:
            pass  # the ignore file is a convenience; the folder is still private
        else:
            with os.fdopen(gi, "wb") as f:
                f.write(b"*\n")
        finally:
            os.close(parent)
        return os.fstat(got[0])
    finally:
        os.close(got[0])


#: how deep a work folder is looked into or deleted (one descriptor per level), and entries a delete takes
WORKDIR_DEPTH = 64
WORKDIR_DELETE_ENTRIES = 200_000


def _child(dfd: int, name: str, listed: os.stat_result) -> int:
    """The directory ``name`` below ``dfd`` (``O_NOFOLLOW``), which must still be the one that was listed."""
    fd = os.open(name, _DIR, dir_fd=dfd)
    try:
        st = os.fstat(fd)
    except OSError:
        os.close(fd)
        raise
    if (st.st_dev, st.st_ino) != (listed.st_dev, listed.st_ino):
        os.close(fd)
        raise OSError(errno.ESTALE, "changed while it was read")
    return fd


def _same_disk(root: int) -> int:
    """The device of the work folder, which must be that of ``.orch-work`` too (no mount at ``.orch-work/<KEY>``)."""
    dev = os.fstat(root).st_dev
    if os.stat("..", dir_fd=root).st_dev != dev:
        raise LayoutError("the work folder is another disk or mount; refusing to use it")
    return dev


def workdir_state(layout: Layout, key: str) -> dict[str, bool]:
    """``{exists, empty}`` of the ticket's work folder, read through descriptors (no link followed). Absent: not
    existing and empty. :class:`LayoutError`: as :func:`workdir_stat`."""
    got = _walk_workdir(layout, key, create=False)
    if got is None:
        return {"exists": False, "empty": True}
    try:
        with os.scandir(got[0]) as it:
            return {"exists": True, "empty": next(it, None) is None}
    except OSError:
        return {"exists": True, "empty": False}  # unreadable counts as used
    finally:
        os.close(got[0])


def workdir_summary(layout: Layout, key: str, *, limit: int = 20000) -> dict[str, int] | None:
    """``{files, bytes, truncated}`` of the work folder (links counted as nothing, not followed). ``truncated``: more
    than ``limit`` entries, deeper than :data:`WORKDIR_DEPTH`, another disk, or a folder that could not be read. Each
    directory is opened relative to its parent's descriptor (linear cost). :class:`LayoutError`: as workdir_stat."""
    got = _walk_workdir(layout, key, create=False)
    if got is None:
        return None
    root = got[0]
    tot = {"files": 0, "bytes": 0, "seen": 0, "truncated": 0}
    try:
        dev = _same_disk(root)

        def walk(dfd: int, depth: int) -> None:
            try:
                with os.scandir(dfd) as it:
                    for e in it:
                        tot["seen"] += 1
                        if tot["seen"] > limit:
                            tot["truncated"] = 1
                            return
                        try:
                            st = e.stat(follow_symlinks=False)
                        except OSError:
                            tot["truncated"] = 1
                            continue
                        if stat.S_ISREG(st.st_mode):
                            tot["files"] += 1
                            tot["bytes"] += st.st_size
                        elif stat.S_ISDIR(st.st_mode):
                            if st.st_dev != dev or depth >= WORKDIR_DEPTH:
                                tot["truncated"] = 1
                                continue
                            try:
                                sub = _child(dfd, e.name, st)
                            except OSError:
                                tot["truncated"] = 1
                                continue
                            try:
                                walk(sub, depth + 1)
                            finally:
                                os.close(sub)
                            if tot["truncated"] and tot["seen"] > limit:
                                return
            except OSError:
                tot["truncated"] = 1

        walk(root, 0)
    finally:
        os.close(root)
    return {"files": tot["files"], "bytes": tot["bytes"], "truncated": tot["truncated"]}


class WorkdirTooBig(LayoutError):
    """A work file is over the cap given to :func:`workdir_read` (``size`` is its size, or the cap + 1 if it grew)."""

    def __init__(self, message: str, size: int) -> None:
        super().__init__(message)
        self.size = size


def workdir_files(
    layout: Layout,
    key: str,
    *,
    limit: int = 20,
    scan: int = 5000,
    accept: Callable[[str], bool] | None = None,
) -> tuple[list[tuple[str, int]], bool]:
    """``([(name, bytes)], truncated)``: the regular files directly in the ticket's work folder that ``accept`` takes
    (sorted, at most ``limit``). Links, folders, files with more than one hard link and files not owned by this user are
    left out, nothing is followed. At most ``scan`` entries are looked at; ``truncated`` says more were left unseen or
    unlisted. A missing folder is ``([], False)``. :class:`LayoutError`: as :func:`workdir_stat`."""
    got = _walk_workdir(layout, key, create=False)
    if got is None:
        return [], False
    out: list[tuple[str, int]] = []
    seen = 0
    truncated = False
    uid = os.getuid()
    try:
        with os.scandir(got[0]) as it:
            for e in it:
                seen += 1
                if seen > scan:
                    truncated = True
                    break
                if accept is not None and not accept(e.name):
                    continue
                try:
                    st = e.stat(follow_symlinks=False)
                except OSError:
                    continue
                if stat.S_ISREG(st.st_mode) and st.st_nlink == 1 and st.st_uid == uid:
                    out.append((e.name, st.st_size))
    except OSError:
        return [], False
    finally:
        os.close(got[0])
    out.sort()
    if len(out) > limit:
        truncated = True
    return out[:limit], truncated


def workdir_read(layout: Layout, key: str, name: str, max_bytes: int) -> bytes:
    """The bytes of ONE file directly in the ticket's work folder: ``name`` is a plain name (no separator, not ``.`` or
    ``..``), opened relative to the folder's descriptor with ``O_NOFOLLOW`` and ``O_NONBLOCK`` (a link is refused, a
    FIFO never blocks), a regular file owned by this user with one hard link, at most ``max_bytes`` (checked before a
    byte is read, and again on the read: :class:`WorkdirTooBig`). :class:`LayoutError` for every other refusal."""
    if not name or "/" in name or "\\" in name or "\0" in name or name in (".", ".."):
        raise LayoutError("that is not a file name in the work folder")
    got = _walk_workdir(layout, key, create=False)
    if got is None:
        raise LayoutError("the work folder is not there")
    try:
        try:
            fd = os.open(name, _FILE, dir_fd=got[0])
        except OSError:
            raise LayoutError(f"{name[:60]} is not a file in the work folder") from None
        try:
            st = os.fstat(fd)
            if not stat.S_ISREG(st.st_mode) or st.st_nlink != 1 or st.st_uid != os.getuid():
                raise LayoutError(f"{name[:60]} is not a plain file")
            if st.st_size > max_bytes:
                raise WorkdirTooBig(f"{name[:60]} is too big", st.st_size)
            with os.fdopen(fd, "rb", closefd=False) as handle:
                data = handle.read(max_bytes + 1)
        finally:
            os.close(fd)
    finally:
        os.close(got[0])
    if len(data) > max_bytes:
        raise WorkdirTooBig(f"{name[:60]} is too big", max_bytes + 1)
    return data


def _after_look() -> None:
    """A seam between the scan and the delete (tests change the folder here)."""


def delete_workdir(layout: Layout, key: str) -> bool:
    """Delete exactly the ticket's work folder (and what is in it), never following a link. ``False``: there was none.
    Nothing outside ``<project>/.orch-work/<KEY>`` is touched. A first pass only looks (another disk or mount, deeper
    than :data:`WORKDIR_DEPTH`, more than :data:`WORKDIR_DELETE_ENTRIES` entries: :class:`LayoutError`, nothing
    deleted); the second deletes, each directory opened relative to its parent and re-checked against what was listed.
    Any failure is a plain :class:`LayoutError`."""
    got = _walk_workdir(layout, key, create=False)
    if got is None:
        return False
    root = got[0]
    try:
        try:
            dev = _same_disk(root)
            count = [0]
            seen: dict[tuple[str, ...], dict[str, tuple[int, int]]] = {}  # directory -> {name: (dev, ino)} as looked at

            def look(dfd: int, depth: int, where: tuple[str, ...]) -> None:
                with os.scandir(dfd) as it:
                    entries = list(it)
                count[0] += len(entries)
                if count[0] > WORKDIR_DELETE_ENTRIES:
                    raise LayoutError("the work folder holds too many files to delete here; delete it in Finder")
                known = seen[where] = {}
                for e in entries:
                    st = e.stat(follow_symlinks=False)
                    known[e.name] = (st.st_dev, st.st_ino)
                    if not stat.S_ISDIR(st.st_mode):
                        continue
                    if st.st_dev != dev:
                        raise LayoutError(f"{e.name} is another disk or mount inside the folder; nothing deleted")
                    if depth >= WORKDIR_DEPTH:
                        raise LayoutError("the work folder is nested too deeply to delete here; delete it in Finder")
                    sub = _child(dfd, e.name, st)
                    try:
                        look(sub, depth + 1, (*where, e.name))
                    finally:
                        os.close(sub)

            def remove(dfd: int, depth: int, where: tuple[str, ...]) -> None:
                """Deletes only what ``look`` recorded, unchanged: anything else refuses."""
                known = seen.get(where)
                if known is None or depth > WORKDIR_DEPTH:
                    raise LayoutError("the work folder changed while it was being deleted; nothing more was deleted")
                with os.scandir(dfd) as it:
                    entries = list(it)
                for e in entries:
                    st = e.stat(follow_symlinks=False)
                    if known.get(e.name) != (st.st_dev, st.st_ino):
                        raise LayoutError(
                            "the work folder changed while it was being deleted; nothing more was deleted"
                        )
                    if stat.S_ISDIR(st.st_mode):
                        if st.st_dev != dev:
                            raise LayoutError(f"{e.name} is another disk or mount inside the folder; not deleted")
                        sub = _child(dfd, e.name, st)
                        try:
                            if os.fstat(sub).st_dev != dev:
                                raise LayoutError(f"{e.name} is another disk or mount inside the folder; not deleted")
                            remove(sub, depth + 1, (*where, e.name))
                        finally:
                            os.close(sub)
                        os.rmdir(e.name, dir_fd=dfd)
                    else:
                        os.unlink(e.name, dir_fd=dfd)

            look(root, 0, ())
            _after_look()
            remove(root, 0, ())
        except LayoutError:
            raise
        except RecursionError:
            raise LayoutError("the work folder is nested too deeply to delete here") from None
        except OSError as exc:
            raise LayoutError(f"the work folder could not be emptied ({exc.strerror})") from None
    finally:
        os.close(root)
    pfd = os.open(layout.project, _DIR)
    try:
        wfd = os.open(WORK_DIR, _DIR, dir_fd=pfd)
        try:
            os.rmdir(key, dir_fd=wfd)
        except OSError as exc:
            raise LayoutError(f"the work folder could not be removed ({exc.strerror})") from None
        finally:
            os.close(wfd)
    finally:
        os.close(pfd)
    return True
