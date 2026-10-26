"""Watching for file system access outside the workspace (#516): an audit hook (installed once per process, idle unless
a :func:`watch` block is open) that records every path event whose real path lies under a watched directory.

Opens relative to a directory fd carry only a name and are skipped: they are the fd walk itself, which never names an
absolute path. Anything the store reaches *by path* through a symlink resolves under the watched directory and is
recorded, whether it was an open, a listing, a rename, an unlink or a mkdir."""

from __future__ import annotations

import contextlib
import os
import sys
from collections.abc import Iterator
from pathlib import Path

_EVENTS = {
    "open": 0,
    "os.listdir": 0,
    "os.scandir": 0,
    "os.mkdir": 0,
    "os.remove": 0,
    "os.rmdir": 0,
    "os.rename": (0, 1),
    "os.truncate": 0,
    "os.chmod": 0,
    "os.utime": 0,
    "shutil.rmtree": 0,
    "glob.glob": 0,
    "glob.glob/2": 0,
}
_watch: list[tuple[str, list[str]]] = []
_installed = False


def _hook(event: str, args: tuple) -> None:
    if not _watch or event not in _EVENTS:
        return
    idx = _EVENTS[event]
    for i in idx if isinstance(idx, tuple) else (idx,):
        p = args[i] if len(args) > i else None
        if p is None or isinstance(p, int):
            continue
        try:
            s = os.fsdecode(p)
        except TypeError:
            continue
        if not s.startswith("/"):
            continue  # relative to a dir fd: the walk itself
        real = os.path.realpath(s)
        for base, seen in _watch:
            if real == base or real.startswith(base + "/"):
                seen.append(f"{event} {s} -> {real}")


@contextlib.contextmanager
def watch(*dirs: Path) -> Iterator[list[str]]:
    """Record path events that resolve under any of ``dirs`` while the block runs; yields the list."""
    global _installed
    if not _installed:
        sys.addaudithook(_hook)
        _installed = True
    seen: list[str] = []
    entries = [(os.path.realpath(d), seen) for d in dirs]
    _watch.extend(entries)
    try:
        yield seen
    finally:
        for e in entries:
            _watch.remove(e)


def snapshot(d: Path) -> dict[str, bytes | None]:
    """Every entry below ``d`` with its bytes (``None`` for a directory), to show nothing there changed."""
    out: dict[str, bytes | None] = {}
    for dirpath, dirnames, filenames in os.walk(d):
        for n in dirnames:
            out[os.path.relpath(os.path.join(dirpath, n), d)] = None
        for n in filenames:
            p = os.path.join(dirpath, n)
            out[os.path.relpath(p, d)] = Path(p).read_bytes()
    return out


def swap(path: Path, target: Path, away: Path) -> None:
    """Move the directory ``path`` to ``away`` and put a symlink to ``target`` in its place."""
    path.rename(away)
    os.symlink(target, path)
