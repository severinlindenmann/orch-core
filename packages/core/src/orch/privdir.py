"""One way to make an orch-owned private directory: mode 0700 whatever the umask (a 022 umask must not leave 0755,
and a directory some other tool, e.g. launchd opening a log, created loose is tightened)."""

from __future__ import annotations

import contextlib
import os
import stat
import time
from pathlib import Path

from orch.posixmod import posixmod

pwd = posixmod("pwd")
grp = posixmod("grp")

__all__ = ["make_private_dir", "others_can_write", "tighten"]

_WRITE_OTHER = 0o002
_WRITE_GROUP = 0o020
_CACHE_SECONDS = 30.0
_cache: dict[tuple[int, int], tuple[float, bool]] = {}


def _private_group_lookup(uid: int, gid: int) -> bool:
    """Is ``gid`` a group only ``uid`` belongs to: their primary group, no other account has it as primary group, and
    the group lists no member but them, and it is named after them (a user-private group; a shared primary group such as
    ``users`` looks empty when enumeration is off). Group passwords/``newgrp`` via gshadow are not consulted: a
    private group has no password. The rule applies to files too where a caller uses it (discovery ``config.json``,
    gitsafe ``.git/config``). Any failure to read the account databases (NSS, LDAP, no pwd/grp module) is
    "not private": the caller then refuses. The answer is cached for 30 s, so a group change (someone added to the
    group, a new account with that primary group) can take up to 30 s to be noticed."""
    try:
        me = pwd.getpwuid(uid)
        if me.pw_gid != gid:
            return False
        if any(p.pw_gid == gid and p.pw_uid != uid for p in pwd.getpwall()):
            return False
        group = grp.getgrgid(gid)
        return group.gr_name == me.pw_name and all(m == me.pw_name for m in group.gr_mem)
    except Exception:  # noqa: BLE001 - fail closed on anything
        return False


def _is_private_group(uid: int, gid: int) -> bool:
    key = (uid, gid)
    now = time.monotonic()
    hit = _cache.get(key)
    if hit is not None and now - hit[0] < _CACHE_SECONDS:
        return hit[1]
    value = _private_group_lookup(uid, gid)
    _cache[key] = (now, value)
    return value


def others_can_write(st: os.stat_result, uid: int) -> bool:
    """The ONE answer to "can a principal other than ``uid`` change this person-owned directory?". The other-write bit
    always counts. The group-write bit counts unless the directory is ``uid``'s own and its group is ``uid``'s private
    group (stock Ubuntu's 0775 under a 002 umask), which nobody else belongs to. Root-owned paths and key files do not
    use this: they keep the strict ``0o022`` rule."""
    mode = stat.S_IMODE(st.st_mode)
    if mode & _WRITE_OTHER:
        return True
    if not mode & _WRITE_GROUP:
        return False
    return not (st.st_uid == uid and _is_private_group(uid, st.st_gid))


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
