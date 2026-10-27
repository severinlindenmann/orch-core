"""One way to make an orch-owned private directory: mode 0700 whatever the umask (a 022 umask must not leave 0755,
and a directory some other tool, e.g. launchd opening a log, created loose is tightened)."""

from __future__ import annotations

import contextlib
import os
import stat
from pathlib import Path

__all__ = ["make_private_dir", "tighten"]


def tighten(path: str | os.PathLike[str]) -> bool:
    """chmod an existing directory of ours that group/others can enter or read to 0700. True when it changed.
    A directory of another user, a link or a non-directory is left alone."""
    try:
        st = os.lstat(path)
    except OSError:
        return False
    if not stat.S_ISDIR(st.st_mode) or (hasattr(os, "getuid") and st.st_uid != os.getuid()):
        return False
    if st.st_mode & 0o077:
        with contextlib.suppress(OSError):
            os.chmod(path, 0o700)
            return True
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
