"""The reader child's own limits and audit hook (D-P2-19, section 3 "Reader profiles", F10).

:func:`apply_limits` runs first thing in the child (the launcher's argv is fixed and the child is orch's own code, so
the child sets the section 0.11 limits on itself rather than through ``preexec_fn``, which is unsafe in the threaded
host): CPU 10 s, address space 512 MiB, 64 open files, no core files, no file writes, no new processes, and a 30 s
``alarm`` (whose default action ends the child). The host enforces the same 30 s wall clock from outside (it kills
the child's process group).

:func:`install` adds the audit hook, after everything the child needs is imported and before it opens anything of the
repository:

* no process, no ``ctypes``, no socket, no write, rename, removal or directory change;
* ``open`` of a path only under the profile's directories (``gitdir``: the pinned gitdir and ``commondir``;
  ``worktree``: those and the pinned working copy; ``pin``: the declared repo paths), compared component by
  component, with no ``..``; a proper ancestor of one of them only as a directory (the ``O_NOFOLLOW`` walk from
  ``/`` passes through it; nothing is read there);
* a relative ``open`` only from :func:`orch.gitread.fs._openat` (recognised by its code object), with ``O_NOFOLLOW``
  and one component, judged by the logical path it carries (directory path plus name);
* a listing only of an fd (already opened under these rules) or of an allowed absolute path.

An ``open`` outside the pins raises ``PermissionError`` in the child; the request answers ``observe.unavailable``.
Like the host's hook this is defence in depth: the real boundary is the fd walk with ``O_NOFOLLOW`` and the pin
checks of :mod:`orch.gitread.fs`, and the OS limits.
"""

from __future__ import annotations

import os
import sys
from typing import Any

from . import limits

__all__ = ["apply_limits", "install", "make_hook"]

_DENY = {
    "subprocess.Popen",
    "os.posix_spawn",
    "os.spawn",
    "os.exec",
    "os.system",
    "os.fork",
    "os.forkpty",
    "pty.spawn",
    "ctypes.dlopen",
    "ctypes.dlsym",
    "ctypes.call_function",
    "socket.__new__",
    "socket.connect",
    "socket.bind",
    "os.chdir",
    "os.chmod",
    "os.chown",
    "os.link",
    "os.mkdir",
    "os.mkfifo",
    "os.mknod",
    "os.remove",
    "os.rename",
    "os.rmdir",
    "os.symlink",
    "os.truncate",
    "os.utime",
    "os.kill",
    "os.killpg",
    "shutil.chown",
    "shutil.copyfile",
    "shutil.copymode",
    "shutil.copystat",
    "shutil.copytree",
    "shutil.move",
    "shutil.rmtree",
    "mmap.__new__",
}
_WRITE_FLAGS = os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND


def apply_limits() -> dict[str, bool]:
    """Set the section 0.11 limits on this process. Returns which ones took (a platform may refuse one, for example
    the address-space limit on macOS)."""
    import resource

    wanted = {
        "RLIMIT_CPU": (limits.CPU_SECONDS, limits.CPU_SECONDS + 1),
        "RLIMIT_AS": (limits.ADDRESS_SPACE, limits.ADDRESS_SPACE),
        "RLIMIT_NOFILE": (limits.OPEN_FILES, limits.OPEN_FILES),
        "RLIMIT_CORE": (0, 0),
        "RLIMIT_FSIZE": (0, 0),
        "RLIMIT_NPROC": (0, 0),
    }
    done: dict[str, bool] = {}
    import signal

    # the wall clock in the child too (the host also kills the group at 30 s): SIGALRM's default action ends it
    signal.alarm(int(limits.WALL_SECONDS))
    done["alarm"] = True
    for name, (soft, hard) in wanted.items():
        res = getattr(resource, name, None)
        if res is None:
            done[name] = False
            continue
        try:
            _, cur_hard = resource.getrlimit(res)
            if cur_hard != resource.RLIM_INFINITY:
                soft, hard = min(soft, cur_hard), min(hard, cur_hard)
            resource.setrlimit(res, (soft, hard))
            done[name] = True
        except (ValueError, OSError):
            done[name] = False
    return done


def _parts(path: str) -> tuple[str, ...] | None:
    if "\0" in path or not path.startswith("/"):
        return None
    parts = path.split("/")[1:]
    if parts and parts[-1] == "":
        parts = parts[:-1]
    if any(c in ("", ".", "..") for c in parts):
        return None
    return tuple(parts)


def make_hook(allowed: list[str]) -> Any:
    from . import fs

    roots = [p for p in (_parts(a) for a in allowed) if p is not None]
    openat_code = fs._openat.__code__
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    directory = getattr(os, "O_DIRECTORY", 0)

    def inside(path: Any) -> bool:
        if not isinstance(path, str):
            return False
        parts = _parts(path)
        return parts is not None and any(parts[: len(r)] == r for r in roots)

    def ancestor(path: Any) -> bool:
        """A proper ancestor of a root: the O_NOFOLLOW walk from ``/`` passes through it (directories only)."""
        if path == "/":
            return bool(roots)
        parts = _parts(path) if isinstance(path, str) else None
        return parts is not None and any(len(parts) < len(r) and r[: len(parts)] == parts for r in roots)

    def hook(event: str, args: tuple) -> None:
        if event in _DENY or event.startswith("os.exec") or event.startswith("os.spawn"):
            raise PermissionError(f"gitread: {event} is not allowed")
        if event == "open":
            path, _mode, flags = args
            if path is None or isinstance(path, int):
                return
            flags = flags or 0
            if flags & _WRITE_FLAGS:
                raise PermissionError("gitread: no writes")
            try:
                p = os.fsdecode(path)
            except TypeError:
                raise PermissionError("gitread: bad path") from None
            is_dir = bool(flags & directory)
            if p.startswith("/"):
                if not inside(p) and not (is_dir and ancestor(p)):
                    raise PermissionError(f"gitread: open outside the profile: {p!r}")
                return
            f = sys._getframe(1)
            if f.f_code is not openat_code or not flags & nofollow or "/" in p:
                raise PermissionError("gitread: a relative open outside the reader's walk")
            loc = f.f_locals
            base = loc.get("base")
            if loc.get("name") != p or not isinstance(base, str):
                raise PermissionError("gitread: open outside the profile")
            full = base.rstrip("/") + "/" + p
            if not inside(full) and not (is_dir and ancestor(full)):
                raise PermissionError("gitread: open outside the profile")
            return
        if event in ("os.listdir", "os.scandir"):
            path = args[0] if args else None
            if isinstance(path, int):
                return
            if not inside(os.fsdecode(path) if path is not None else None):
                raise PermissionError("gitread: listing outside the profile")

    return hook


def install(allowed: list[str]) -> None:
    """Install the hook for good (audit hooks cannot be removed)."""
    sys.addaudithook(make_hook(allowed))
