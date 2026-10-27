"""One way to make an orch-owned private directory: mode 0700 whatever the umask (a 022 umask must not leave 0755,
and a directory some other tool, e.g. launchd opening a log, created loose is tightened)."""

from __future__ import annotations

import contextlib
import os
import stat
from pathlib import Path

__all__ = ["make_private_dir", "tighten"]


def healable(mode: int) -> bool:
    """The ONE rule for a too-open private directory of ours (custody and the creators share it): only read/execute
    for group/others is tightened (launchd makes 0744). Writable by them may hold planted files: never healed."""
    return bool(mode & 0o077) and not mode & 0o022


def tighten(path: str | os.PathLike[str]) -> bool:
    """chmod an existing directory of ours that group/others can read or enter (not write) to 0700, through an
    ``O_NOFOLLOW`` descriptor. True when it changed; anything else (another user's, a link, writable by others) is
    left alone for custody to refuse."""
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0))
    except OSError:
        return False
    try:
        st = os.fstat(fd)
        if not stat.S_ISDIR(st.st_mode) or (hasattr(os, "getuid") and st.st_uid != os.getuid()):
            return False
        if healable(st.st_mode):
            os.fchmod(fd, 0o700)
            return True
    except OSError:
        pass
    finally:
        os.close(fd)
    return False


def make_private_dir(path: str | os.PathLike[str]) -> Path:
    """Create ``path`` (and the missing parents) so every directory this call makes is 0700 regardless of the umask;
    an existing ``path`` of ours is tightened to 0700. Existing parents are untouched."""
    p = Path(path)
    missing: list[Path] = []
    cur = p
    while not cur.exists() and cur != cur.parent:
        missing.append(cur)
        cur = cur.parent
    for d in reversed(missing):
        with contextlib.suppress(FileExistsError):
            os.mkdir(d, 0o700)
        with contextlib.suppress(OSError):
            os.chmod(d, 0o700)  # umask-proof (the mkdir mode is masked)
    tighten(p)
    return p
