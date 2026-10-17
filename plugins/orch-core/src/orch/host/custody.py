"""D-P2-17 custody checks for the host's own files: the registry, intents, keys and workspace roots.

Every path is walked from ``/`` one component at a time with ``O_NOFOLLOW|O_DIRECTORY``, so a symlink anywhere on
the way is refused. An ancestor must be owned by root or the host UID, with no group or other write unless it is a
root-owned sticky directory (``/tmp``), and no ACL that grants write to anyone else. A child of a sticky directory
must itself be owned by root or the host UID (the sticky bit keeps others from replacing it). The host tightens the
modes of its own directories; it never repairs foreign ownership.
"""

from __future__ import annotations

import contextlib
import json
import os
import stat
from pathlib import Path
from typing import Any

from orch.layout import STORE_DIR
from orch.store.fsio import write_atomic

from ._unix import UnsafePath, check_acl
from .errors import HostError

__all__ = [
    "check_tree",
    "check_workspace_files",
    "ensure_private_dir",
    "read_json",
    "root_key",
    "store_name",
    "same_or_inside",
    "unsafe",
    "write_private",
]

_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC


def root_key(path: str | os.PathLike[str]) -> tuple[int, int] | str:
    """What identifies a directory: ``(st_dev, st_ino)``, so case and Unicode-normalisation variants of one path on
    a case-insensitive or normalising volume (APFS) are the same root. A path that does not exist is its string."""
    try:
        info = os.stat(path)
    except OSError:
        return os.fspath(path)
    return (info.st_dev, info.st_ino)


def same_or_inside(path: str | os.PathLike[str], base: str | os.PathLike[str]) -> bool:
    """Is ``path`` the directory ``base`` or below it, compared by identity (each existing ancestor of ``path``)?"""
    target = root_key(base)
    if isinstance(target, str):
        return False
    current = Path(path)
    for item in (current, *current.parents):
        if root_key(item) == target:
            return True
    return False


def unsafe(message: str) -> HostError:
    return HostError("custody.unsafe_mode", message)


def _check_component(fd: int, path: Path, host_uid: int, *, last: bool) -> bool:
    info = os.fstat(fd)
    mode = stat.S_IMODE(info.st_mode)
    if info.st_uid not in {0, host_uid}:
        raise unsafe(f"{path}: owner {info.st_uid}, expected root or {host_uid}")
    sticky = info.st_uid == 0 and bool(mode & stat.S_ISVTX)
    if mode & 0o022 and (last or not sticky):
        raise unsafe(f"{path}: writable by another principal")
    try:
        check_acl(fd, {0, host_uid})
    except UnsafePath as exc:
        raise unsafe(f"{path}: {exc}") from None
    return sticky


def check_tree(path: str | os.PathLike[str], host_uid: int, *, owner_only: bool = True) -> Path:
    """Check ``path`` (absolute, resolved by the caller) and every ancestor; return it.

    With ``owner_only`` the final directory must be owned by the host UID itself, not root.
    """
    target = Path(path)
    if not target.is_absolute() or ".." in target.parts:
        raise unsafe(f"{target}: expected an absolute path without parent traversal")
    fds: list[int] = []
    try:
        fd = os.open("/", _FLAGS)
        fds.append(fd)
        _check_component(fd, Path("/"), host_uid, last=len(target.parts) == 1)
        current = Path("/")
        for index, part in enumerate(target.parts[1:]):
            current /= part
            try:
                fd = os.open(part, _FLAGS, dir_fd=fd)
            except FileNotFoundError:
                raise HostError("not_found", f"{current} does not exist") from None
            except NotADirectoryError:
                raise unsafe(f"{current}: not a directory") from None
            except OSError as exc:
                raise unsafe(f"{current}: {exc.strerror or exc}") from None
            fds.append(fd)
            _check_component(fd, current, host_uid, last=index == len(target.parts) - 2)
        if owner_only and os.fstat(fds[-1]).st_uid != host_uid:
            raise unsafe(f"{target}: not owned by the host UID {host_uid}")
        return target
    finally:
        for item in reversed(fds):
            os.close(item)


def ensure_private_dir(path: Path, host_uid: int, *, create: bool = True) -> Path:
    """A directory private to the host UID (0700), whose ancestors pass :func:`check_tree`.

    An existing directory of the host UID that is only readable by others is tightened to 0700; one that others can
    write is refused.
    """
    if create:
        with contextlib.suppress(FileExistsError):
            os.mkdir(path, 0o700)
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        raise HostError("not_found", f"{path} does not exist") from None
    if not stat.S_ISDIR(info.st_mode):
        raise unsafe(f"{path}: not a directory")
    if info.st_uid != host_uid:
        raise unsafe(f"{path}: owner {info.st_uid}, expected {host_uid}")
    if stat.S_IMODE(info.st_mode) & 0o022:
        raise unsafe(f"{path}: writable by another principal")
    if stat.S_IMODE(info.st_mode) != 0o700:
        fd = os.open(path, _FLAGS)
        try:
            os.fchmod(fd, 0o700)
        finally:
            os.close(fd)
    check_tree(path, host_uid)
    return path


def write_private(path: Path, doc: Any) -> None:
    """Replace ``path`` atomically and durably (fsync file and directory) with ``doc`` as JSON, mode 0600."""
    write_atomic(path, json.dumps(doc, sort_keys=True, separators=(",", ":")).encode(), mode=0o600)


def read_json(path: Path, host_uid: int, *, limit: int = 16 * 1024 * 1024) -> Any | None:
    """A private JSON file of the host UID, or ``None`` if it does not exist. A link, another owner or a group/other
    readable or writable mode is ``custody.unsafe_mode``."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK)  # a FIFO never blocks
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise unsafe(f"{path}: {exc.strerror or exc}") from None
    with os.fdopen(fd, "rb") as handle:
        info = os.fstat(handle.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != host_uid or stat.S_IMODE(info.st_mode) & 0o077:
            raise unsafe(f"{path}: expected a 0600 file of UID {host_uid}")
        raw = handle.read(limit + 1)
    if len(raw) > limit:
        raise unsafe(f"{path}: too large")
    try:
        return json.loads(raw)
    except ValueError:
        raise unsafe(f"{path}: unreadable") from None


def _lstat_owned(path: Path, host_uid: int) -> os.stat_result | None:
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return None
    if stat.S_ISLNK(info.st_mode):
        raise unsafe(f"{path}: a symbolic link")
    if info.st_uid != host_uid:
        raise unsafe(f"{path}: owner {info.st_uid}, expected {host_uid}")
    return info


def check_workspace_files(
    state_dir: Path, workspace_id: str, root: Path, host_uid: int, *, store: str | None = None
) -> None:
    """D-P2-17 for one workspace's custody list, beyond the paths :func:`check_tree` walks.

    * ``hosts/<ws>/keys/`` is 0700 and every key file a regular 0600 file of the host UID (a key others could read
      is already compromised, so it is refused, never repaired);
    * every other file in ``hosts/<ws>/`` (the genesis pin, noted revocations, checkpoints) and ``outcomes/``,
      ``requests/``, ``blobs/`` there: owned by the host UID, writable by nobody else;
    * the workspace's ``events/``, ``tickets/`` and ``.state/``: owned by the host UID, group-readable at most;
    * ``store`` (``".orch"``, the registry entry's key; ``None``: legacy, the store is ``root``): ``root/.orch`` itself
      is a real directory of the host UID that no one else can write (an entry with a store never silently passes
      because the checks looked at the project root), and the three directories above are looked at below it.
    """
    hosts = Path(state_dir) / "hosts" / workspace_id
    keys = hosts / "keys"
    info = _lstat_owned(keys, host_uid)
    if info is not None:
        if not stat.S_ISDIR(info.st_mode) or stat.S_IMODE(info.st_mode) & 0o077:
            raise unsafe(f"{keys}: expected a 0700 directory")
        for name in os.listdir(keys):
            item = _lstat_owned(keys / name, host_uid)
            if item is not None and (not stat.S_ISREG(item.st_mode) or stat.S_IMODE(item.st_mode) & 0o077):
                raise unsafe(f"{keys / name}: a key must be a 0600 file")

    # R6a (11A.15.3): every directory under membership/ is a 0700 directory and every file a 0600 regular file (WK,
    # WXK, CK and S are secrets: refused, never repaired, as the WSK above). Walked to depth 3, so R6c's grants/<e>/,
    # devices/ and pairing/ (and primary.json, alarms.json) are checked by the same rule when they exist; anything
    # deeper is refused.
    def _membership(d: Path, depth: int) -> None:
        info = _lstat_owned(d, host_uid)
        if info is None:
            return
        if not stat.S_ISDIR(info.st_mode) or stat.S_IMODE(info.st_mode) & 0o077:
            raise unsafe(f"{d}: expected a 0700 directory")
        for name in os.listdir(d):
            item = _lstat_owned(d / name, host_uid)
            if item is None:
                continue
            if stat.S_ISDIR(item.st_mode):
                if depth >= 2:
                    raise unsafe(f"{d / name}: membership directories nest at most three deep")
                _membership(d / name, depth + 1)
            elif not stat.S_ISREG(item.st_mode) or stat.S_IMODE(item.st_mode) & 0o077:
                raise unsafe(f"{d / name}: a membership file must be a 0600 file")

    _membership(hosts / "membership", 0)
    if _lstat_owned(hosts, host_uid) is not None:
        for name in os.listdir(hosts):
            item = _lstat_owned(hosts / name, host_uid)
            if item is not None and stat.S_IMODE(item.st_mode) & 0o022:
                raise unsafe(f"{hosts / name}: writable by another principal")
    base = Path(root)
    if store is not None:
        if store != STORE_DIR:
            raise unsafe(f"{store}: not a store directory name")
        base = Path(root) / store
        item = _lstat_owned(base, host_uid)
        if item is None:
            raise unsafe(f"{base}: the registered store directory is missing")
        if not stat.S_ISDIR(item.st_mode) or stat.S_IMODE(item.st_mode) & 0o022:
            raise unsafe(f"{base}: writable by another principal")
    for name in ("events", "tickets", ".state"):
        item = _lstat_owned(base / name, host_uid)
        if item is not None and (not stat.S_ISDIR(item.st_mode) or stat.S_IMODE(item.st_mode) & 0o022):
            raise unsafe(f"{base / name}: writable by another principal")


def store_name(root: str | os.PathLike[str]) -> str | None:
    """The registry ``store`` value of the project at ``root`` as it is on disk (``".orch"``; ``None``: legacy or no
    store yet). A layout that cannot be placed (two stores, a linked ``.orch``) is refused."""
    from orch import layout as layout_mod

    try:
        fd = os.open(root, _FLAGS)
    except OSError as exc:
        raise unsafe(f"{root}: {exc.strerror or exc}") from None
    try:
        found = layout_mod.resolve(fd, root)
    except layout_mod.LayoutError as exc:
        raise unsafe(str(exc)) from None
    finally:
        os.close(fd)
    return None if found is None or found.legacy else STORE_DIR
