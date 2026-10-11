"""The host's audit hook (D-P2-9, section 3): no ``open`` outside the roots, no process spawn but the launcher.

``install(roots)`` is called once at host start (never in direct mode). ``sys.addaudithook`` hooks cannot be removed,
so tests install it in a subprocess.

Spawns. Every ``subprocess.Popen``, ``os.posix_spawn``, ``os.spawn*``, ``os.exec*``, ``os.system``, ``os.fork*`` and
``pty.spawn`` event raises, except a ``subprocess.Popen`` or ``os.posix_spawn`` whose argv is exactly
:func:`orch.host.launcher.gitread_argv` and whose caller is :func:`orch.host.launcher.spawn_gitread` (found by walking
the Python frames: module name and function name). Limit: Python code that fabricates a frame with that module name
could pass the caller test; the argv check still holds, and the argv runs only the fixed reader module under ``-I``.

Opens. The ``open`` event carries a path string, so this check is **not** race-free and is defence in depth: the
capability object (:class:`orch.ops.caps.Files`) is the real boundary, because its check is the open itself
(``dir_fd`` plus ``O_NOFOLLOW``). The hook uses the same single reading as ``Files`` for absolute paths: no
normalisation, no ``realpath``, a component-wise prefix of a root, and ``..``, ``.``, empty components and NUL refused;
so the two cannot disagree about what a path means. Symlinks inside a root are not followed by ``Files``; a plain
``open()`` of a path under a root that is a symlink is checked here only lexically (limit). Relative names are
allowed only in the shape ``Files`` uses (a single component with ``O_NOFOLLOW``, resolved against a ``dir_fd``); the
hook cannot see the ``dir_fd``, so such an open is checked for nothing more (limit). File descriptors (``open(3)``)
pass: they name an object already opened. ``/dev/null`` passes (the launcher's stderr); ``/dev/tty`` is outside
every root and refused. The host imports everything it needs before ``install`` (imports open files too).
"""

from __future__ import annotations

import os
import sys
from collections.abc import Iterable

__all__ = ["install", "make_hook"]

_SPAWN_EVENTS = {
    "subprocess.Popen",
    "os.posix_spawn",
    "os.spawn",
    "os.exec",
    "os.system",
    "os.fork",
    "os.forkpty",
    "pty.spawn",
}
_LAUNCHER_ARGV_EVENTS = {"subprocess.Popen", "os.posix_spawn"}


def _argv_of(event: str, args: tuple) -> list[str] | None:
    # subprocess.Popen: (executable, args, cwd, env); os.posix_spawn: (path, argv, env)
    argv = args[1] if len(args) > 1 else None
    try:
        return [os.fsdecode(a) for a in argv] if argv is not None else None
    except TypeError:
        return None


def _called_from_launcher() -> bool:
    f = sys._getframe(1)
    while f is not None:
        if f.f_globals.get("__name__") == "orch.host.launcher" and f.f_code.co_name == "spawn_gitread":
            return True
        f = f.f_back
    return False


def _split(path: str, roots: list[tuple[str, ...]]) -> bool:
    if "\0" in path or not path.startswith("/"):
        return False
    parts = path.split("/")[1:]
    if parts and parts[-1] == "":
        parts = parts[:-1]
    if any(c in ("", ".", "..") for c in parts):
        return False
    return any(tuple(parts[: len(r)]) == r for r in roots)


def make_hook(roots: Iterable[str | os.PathLike[str]]):
    from orch.host import launcher

    root_parts = [tuple(c for c in os.fspath(r).split("/") if c) for r in roots]
    allowed_argv = launcher.gitread_argv()
    nofollow = getattr(os, "O_NOFOLLOW", 0)

    def hook(event: str, args: tuple) -> None:
        if event in _SPAWN_EVENTS:
            if event in _LAUNCHER_ARGV_EVENTS and _argv_of(event, args) == allowed_argv and _called_from_launcher():
                return
            raise PermissionError(f"host audit: {event} is not allowed")
        if event == "open":
            path, _mode, flags = args[0], args[1], args[2]
            if path is None or isinstance(path, int):
                return
            try:
                p = os.fsdecode(path)
            except TypeError:
                raise PermissionError("host audit: open of a non-path") from None
            if p == os.devnull:  # subprocess.DEVNULL (the launcher's stderr); holds no data
                return
            if "/" not in p and p not in ("", ".", "..") and "\0" not in p and flags is not None and flags & nofollow:
                return  # the dir_fd-relative shape of orch.ops.caps.Files
            if not _split(p, root_parts):
                raise PermissionError(f"host audit: open outside the roots: {p!r}")

    return hook


def install(roots: Iterable[str | os.PathLike[str]]) -> None:
    """Install the hook for this process, for good. ``roots``: the workspace root and the state directory, by their
    real paths."""
    sys.addaudithook(make_hook(roots))
