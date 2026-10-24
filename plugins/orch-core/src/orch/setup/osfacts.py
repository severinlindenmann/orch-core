"""OS facts behind one small adapter (H1 L1/L11): users, groups, path ownership, who can read what, root-owned pins.

The setup plan (``osusers``) and the isolation check (``host.isolation``) read the system only through ``OsFacts``, so
tests use a fake and nothing here ever creates or changes anything. ``RealOsFacts`` is the Linux/POSIX implementation.

Fail closed: a path whose access cannot be proven closed (an ACL entry is present, a stat error other than "not found")
reads as accessible, so a check that wants "not readable by the agent" fails rather than guesses.
"""

from __future__ import annotations

import grp
import os
import pwd
import stat
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

__all__ = ["GroupInfo", "OsFacts", "PathInfo", "RealOsFacts", "UnitState", "UserInfo"]


@dataclass(frozen=True)
class UserInfo:
    name: str
    uid: int
    gid: int
    home: str
    shell: str
    groups: frozenset[str]  # every group the user is in, primary included, by name


@dataclass(frozen=True)
class GroupInfo:
    name: str
    gid: int
    members: frozenset[str]


@dataclass(frozen=True)
class PathInfo:
    uid: int
    gid: int
    mode: int  # st_mode including the type bits
    is_dir: bool
    is_socket: bool
    is_symlink: bool


@dataclass(frozen=True)
class UnitState:
    state: str  # "active" (uid set) | "inactive" (clearly down) | "unknown" (anything unclear)
    uid: int | None = None


class OsFacts(Protocol):
    platform: str  # "linux", "darwin" or "windows"

    def user(self, name: str) -> UserInfo | None: ...
    def user_by_uid(self, uid: int) -> UserInfo | None: ...
    def group(self, name: str) -> GroupInfo | None: ...
    def stat(self, path: str) -> PathInfo | None: ...
    def listdir(self, path: str) -> list[str]: ...
    def can_read(self, uid: int, path: str) -> bool: ...
    def unit_state(self, unit: str) -> UnitState: ...
    def pin(self, uid: int) -> dict | None: ...  # root-owned pin for a UID; None = no pin; broken = a "*" agent pin


def _platform() -> str:
    return {"linux": "linux", "darwin": "darwin", "win32": "windows"}.get(sys.platform, sys.platform)


class RealOsFacts:
    platform = _platform()

    def __init__(self, pin_dir: Path | None = None) -> None:
        self._pin_dir = pin_dir

    def _info(self, entry: pwd.struct_passwd) -> UserInfo:
        groups: set[str] = set()
        for g in grp.getgrall():
            if g.gr_gid == entry.pw_gid or entry.pw_name in g.gr_mem:
                groups.add(g.gr_name)
        return UserInfo(entry.pw_name, entry.pw_uid, entry.pw_gid, entry.pw_dir, entry.pw_shell, frozenset(groups))

    def user(self, name: str) -> UserInfo | None:
        try:
            return self._info(pwd.getpwnam(name))
        except KeyError:
            return None

    def user_by_uid(self, uid: int) -> UserInfo | None:
        try:
            return self._info(pwd.getpwuid(uid))
        except KeyError:
            return None

    def group(self, name: str) -> GroupInfo | None:
        try:
            g = grp.getgrnam(name)
        except KeyError:
            return None
        return GroupInfo(g.gr_name, g.gr_gid, frozenset(g.gr_mem))

    def stat(self, path: str) -> PathInfo | None:
        try:
            st = os.lstat(path)
        except FileNotFoundError:
            return None
        return PathInfo(
            st.st_uid,
            st.st_gid,
            st.st_mode,
            stat.S_ISDIR(st.st_mode),
            stat.S_ISSOCK(st.st_mode),
            stat.S_ISLNK(st.st_mode),
        )

    def listdir(self, path: str) -> list[str]:
        try:
            return sorted(os.listdir(path))
        except OSError:
            return []

    def _gids(self, uid: int) -> set[int]:
        info = self.user_by_uid(uid)
        if info is None:
            return set()
        gids = {info.gid}
        gids.update(g.gr_gid for g in grp.getgrall() if info.name in g.gr_mem)
        return gids

    def _has_acl(self, path: str) -> bool:
        try:
            return any(a.startswith("system.posix_acl") for a in os.listxattr(path, follow_symlinks=False))
        except (OSError, AttributeError):
            return False

    def _allowed(self, st: os.stat_result, path: str, uid: int, gids: set[int], bit: int) -> bool:
        if uid == 0:
            return True
        if self._has_acl(path):
            return True  # cannot prove it closed from mode bits alone
        shift = 6 if st.st_uid == uid else 3 if st.st_gid in gids else 0
        return bool(st.st_mode & (bit << shift))

    def can_read(self, uid: int, path: str) -> bool:
        """True unless ``uid`` provably cannot read ``path`` (search on every ancestor, read on the path)."""
        gids = self._gids(uid)
        p = Path(os.path.abspath(path))
        try:
            for anc in reversed(p.parents):
                if not self._allowed(os.stat(anc), str(anc), uid, gids, 0o1):
                    return False
            return self._allowed(os.stat(p), str(p), uid, gids, 0o4)
        except FileNotFoundError:
            return False
        except OSError:
            return True

    def pin(self, uid: int) -> dict | None:
        from ..host.pins import AgentPins

        return AgentPins(self._pin_dir).get(uid)

    def _show(self, unit: str) -> tuple[str, int]:
        import subprocess

        out = subprocess.run(  # fixed absolute binary, no shell, fixed arguments
            ["/usr/bin/systemctl", "show", "-p", "ActiveState", "-p", "MainPID", "--", unit],
            capture_output=True,
            text=True,
            timeout=5,
            check=True,
            env={},
        ).stdout
        vals = dict(line.split("=", 1) for line in out.splitlines() if "=" in line)
        return vals["ActiveState"], int(vals["MainPID"])

    def unit_state(self, unit: str) -> UnitState:
        """Clearly inactive, or active with the UID its main process really has, else unknown (never a guess)."""
        import subprocess

        try:
            state, pid = self._show(unit)
            if state in ("inactive", "failed") and pid == 0:
                return UnitState("inactive")
            if state != "active" or pid <= 0:
                return UnitState("unknown")  # transitional (activating, deactivating, reloading) or odd
            with open(f"/proc/{pid}/cgroup", encoding="utf-8") as handle:
                if f"/{unit}" not in handle.read():
                    return UnitState("unknown")  # the PID is not in the unit's cgroup (reused, or moved)
            uid = os.stat(f"/proc/{pid}").st_uid
            if self._show(unit) != (state, pid):
                return UnitState("unknown")  # changed while we looked
            return UnitState("active", uid)
        except (OSError, ValueError, KeyError, subprocess.SubprocessError):
            return UnitState("unknown")
