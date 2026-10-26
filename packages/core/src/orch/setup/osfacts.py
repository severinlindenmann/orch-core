"""OS facts behind one small adapter (H1 L1/L11): users, groups, path ownership, who can read what, root-owned pins.

The setup plan (``osusers``) and the isolation check (``host.isolation``) read the system only through ``OsFacts``, so
tests use a fake and nothing here ever creates or changes anything. ``RealOsFacts`` is the Linux/POSIX implementation.

Fail closed: a path whose access cannot be proven closed (an ACL entry is present, a stat error other than "not found")
reads as accessible, so a check that wants "not readable by the agent" fails rather than guesses.
"""

from __future__ import annotations

import errno
import os
import stat
import struct
import sys
from dataclasses import dataclass
from fnmatch import fnmatch
from functools import reduce
from operator import or_
from pathlib import Path
from typing import Protocol

from orch.posixmod import posixmod

grp = posixmod("grp")
pwd = posixmod("pwd")

__all__ = ["GroupInfo", "OsFacts", "PathInfo", "RealOsFacts", "UnitState", "UserInfo"]

_SYSTEMCTL = ("/usr/bin/systemctl", "/bin/systemctl")


@dataclass(frozen=True)
class UserInfo:
    name: str
    uid: int
    gid: int
    home: str
    shell: str
    groups: frozenset[str]  # names of every group the user is in, primary included (for messages only)
    gids: frozenset[int]  # every gid of the user (``os.getgrouplist``), primary included: what checks compare


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
    has_acl: bool = False  # an access or default ACL is present (or could not be ruled out)


@dataclass(frozen=True)
class UnitState:
    # "active" (identity fields set) | "inactive" (clearly down) | "transitional" (activating, deactivating, reloading,
    # auto-restart; identity fields are set when a process runs, none when no process runs) | "unknown" (error, parse
    # failure, inconsistency: not trusted)
    state: str
    uid: int | None = None
    gids: frozenset[int] | None = None  # the process's real/effective gid and ``Groups:`` from /proc/<pid>/status
    no_new_privs: bool | None = None
    cap_eff: int | None = None
    cap_amb: int | None = None
    cap_prm: int | None = None
    cap_inh: int | None = None
    cap_bnd: int | None = None


class OsFacts(Protocol):
    platform: str  # "linux", "darwin" or "windows"

    def user(self, name: str) -> UserInfo | None: ...
    def user_by_uid(self, uid: int) -> UserInfo | None: ...
    def group(self, name: str) -> GroupInfo | None: ...
    def users_with_gid(self, gid: int) -> list[str]: ...  # names of users whose MAIN group is gid
    def stat(self, path: str) -> PathInfo | None: ...
    def listdir(self, path: str) -> list[str]: ...
    def can_read(self, uid: int, path: str) -> bool: ...  # True unless provably unreadable (dirs: r or x counts)
    def can_connect(self, uid: int, path: str) -> bool: ...  # write on the socket and search on every ancestor
    def can_stat(self, uid: int, path: str) -> bool: ...  # search (x) on every folder above ``path``: uid can stat it
    # Root-written policy is never parsed here. A root process asks the authoritative tools (setup, L2); the host only
    # compares stat signatures with what the root-owned pin recorded.
    def sudo_allows(self, user: str) -> bool | None: ...  # ``sudo -l -U``: True = a rule, False = none
    def sshd_installed(self) -> bool: ...
    def sshd_ports(self) -> list[int] | None: ...  # the ports sshd itself reports; None = unknown
    def sshd_denies(self, user: str, host: str, addr: str, laddr: str, lport: int) -> bool | None: ...
    # file_sig: (size, mtime_ns, ctime_ns, inode); None = absent; UNREADABLE (never equals a record) = stat failed
    def file_sig(self, path: str) -> tuple[int, int, int, int] | None: ...
    def unit_state(self, unit: str) -> UnitState: ...
    def pin(self, uid: int) -> dict | None: ...  # root-owned pin for a UID; None = no pin; broken = a "*" agent pin


def _platform() -> str:
    return {"linux": "linux", "darwin": "darwin", "win32": "windows"}.get(sys.platform, sys.platform)


UNREADABLE = (-1, -1, -1, -1)  # a signature that can never match a recorded one
_SSHD = ("/usr/sbin/sshd", "/sbin/sshd")
_ACL_ACCESS = "system.posix_acl_access"
_ACL_NAMES = (_ACL_ACCESS, "system.posix_acl_default")


class _Unprovable(Exception):
    """A permission fact could not be established: callers fail closed."""


class RealOsFacts:
    platform = _platform()

    def __init__(self, pin_dir: Path | None = None) -> None:
        self._pin_dir = pin_dir

    def _info(self, entry: pwd.struct_passwd) -> UserInfo:
        names = {g.gr_name for g in grp.getgrall() if g.gr_gid == entry.pw_gid or entry.pw_name in g.gr_mem}
        gids = frozenset(os.getgrouplist(entry.pw_name, entry.pw_gid))  # also sees sssd/LDAP and pam_group
        names |= {n for n in (self._gname(g) for g in gids) if n}
        return UserInfo(entry.pw_name, entry.pw_uid, entry.pw_gid, entry.pw_dir, entry.pw_shell, frozenset(names), gids)

    @staticmethod
    def _gname(gid: int) -> str | None:
        try:
            return grp.getgrgid(gid).gr_name
        except KeyError:
            return None

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

    def users_with_gid(self, gid: int) -> list[str]:
        return [e.pw_name for e in pwd.getpwall() if e.pw_gid == gid]

    def _has_acl(self, path: str) -> bool:
        try:
            return any(a in _ACL_NAMES for a in os.listxattr(path, follow_symlinks=False))
        except AttributeError:
            return False
        except OSError as exc:
            return exc.errno not in (errno.ENOTSUP, errno.ENOENT)  # cannot tell: assume an ACL

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
            self._has_acl(path),
        )

    def listdir(self, path: str) -> list[str]:
        try:
            return sorted(os.listdir(path))
        except OSError:
            return []

    def _acl_bytes(self, path: str) -> bytes | None:
        try:
            return os.getxattr(path, _ACL_ACCESS)
        except AttributeError:
            return None
        except OSError as exc:
            if exc.errno in (errno.ENODATA, errno.ENOTSUP):
                return None
            raise _Unprovable(path) from exc

    def _perm(self, st: os.stat_result, path: str, uid: int, gids: frozenset[int]) -> int:
        """The rwx bits (0-7) ``uid`` with ``gids`` gets on ``path``: POSIX ACL algorithm when an ACL exists."""
        raw = self._acl_bytes(path)
        if raw is None:
            if st.st_uid == uid:
                return (st.st_mode >> 6) & 7
            return (st.st_mode >> 3) & 7 if st.st_gid in gids else st.st_mode & 7
        try:
            if len(raw) < 4 or struct.unpack_from("<I", raw)[0] != 2 or (len(raw) - 4) % 8:
                raise _Unprovable(path)
            entries = [struct.unpack_from("<HHI", raw, 4 + i) for i in range(0, len(raw) - 4, 8)]
        except struct.error as exc:
            raise _Unprovable(path) from exc
        owner = group_obj = other = 0
        users: dict[int, int] = {}
        groups: dict[int, int] = {}
        mask = 7
        for tag, perm, ident in entries:
            if tag == 0x01:
                owner = perm
            elif tag == 0x02:
                users[ident] = perm
            elif tag == 0x04:
                group_obj = perm
            elif tag == 0x08:
                groups[ident] = perm
            elif tag == 0x10:
                mask = perm
            elif tag == 0x20:
                other = perm
        if st.st_uid == uid:
            return owner & 7
        if uid in users:
            return users[uid] & mask & 7
        matched = [p for g, p in groups.items() if g in gids]
        if st.st_gid in gids:
            matched.append(group_obj)
        if matched:
            return reduce(or_, matched, 0) & mask & 7
        return other & 7

    @staticmethod
    def _ancestors(real: str) -> list[Path]:
        return list(reversed(Path(real).parents))

    def _reach(self, uid: int, path: str, ancestor_bits: int, final_file_bits: int, final_dir_bits: int) -> bool:
        """True unless provably denied. Every ancestor of the real path (symlinks resolved) must give one of
        ``ancestor_bits``; the target one of the final bits. ACL entries are evaluated, not assumed open."""
        info = self.user_by_uid(uid)
        gids = info.gids if info else frozenset()
        try:
            real = os.path.realpath(path)
            for anc in self._ancestors(real):
                if not self._perm(os.stat(anc), str(anc), uid, gids) & ancestor_bits:
                    return False
            st = os.stat(real)
            bits = final_dir_bits if stat.S_ISDIR(st.st_mode) else final_file_bits
            return bool(self._perm(st, real, uid, gids) & bits)
        except FileNotFoundError:
            return False
        except (OSError, _Unprovable):
            return True

    def can_read(self, uid: int, path: str) -> bool:
        """r or x on every ancestor (a 0711 directory still lets a known file name be opened), r on a file, r or x on
        a directory."""
        return uid == 0 or self._reach(uid, path, 0o5, 0o4, 0o5)

    def can_stat(self, uid: int, path: str) -> bool:
        """Whether ``uid`` can stat ``path``: x on every real ancestor (ACLs evaluated). Unprovable counts as yes, so
        the caller keeps watching the entry (an entry the host then cannot stat reads as changed: fail closed)."""
        if uid == 0:
            return True
        info = self.user_by_uid(uid)
        gids = info.gids if info else frozenset()
        try:
            # the folders above the ENTRY (not what it points to): a symlinked drop-in is still stat-ed by its own name,
            # and the host then fails on a root-only target, so it stays recorded and reads as changed (fail closed)
            entry = os.path.join(os.path.realpath(os.path.dirname(path)), os.path.basename(path))
            for anc in self._ancestors(entry):
                if not self._perm(os.stat(anc), str(anc), uid, gids) & 0o1:
                    return False
        except (OSError, _Unprovable):
            return True
        return True

    def can_connect(self, uid: int, path: str) -> bool:
        return uid == 0 or self._reach(uid, path, 0o1, 0o2, 0o2)

    def file_sig(self, path: str) -> tuple[int, int, int, int] | None:
        try:
            st = os.stat(path)
        except FileNotFoundError:
            return None
        except OSError:  # e.g. PermissionError inside a 0750/0700 directory on RHEL: never matches a record
            return UNREADABLE
        return (st.st_size, st.st_mtime_ns, st.st_ctime_ns, st.st_ino)

    @staticmethod
    def _run(argv: list[str]) -> tuple[int, str] | None:
        import subprocess

        try:
            done = subprocess.run(argv, capture_output=True, text=True, timeout=10, check=False, env={})
        except (OSError, subprocess.SubprocessError):
            return None
        return done.returncode, done.stdout + done.stderr

    def sudo_allows(self, user: str) -> bool | None:
        """sudo's own verdict for ``user`` (``sudo -l -U``; needs root). No sudo installed means no sudo rule."""
        sudo = next((b for b in ("/usr/bin/sudo", "/bin/sudo") if os.path.exists(b)), None)
        if sudo is None:
            return False
        res = self._run([sudo, "-n", "-l", "-U", user])
        if res is None:
            return None
        if "is not allowed to run sudo" in res[1]:
            return False
        if "may run the following commands" in res[1]:
            return True
        return None

    def sshd_installed(self) -> bool:
        return any(os.path.exists(b) for b in _SSHD)

    def sshd_ports(self) -> list[int] | None:
        sshd = next((b for b in _SSHD if os.path.exists(b)), None)
        if sshd is None:
            return []
        res = self._run([sshd, "-T"])
        if res is None or res[0] != 0:
            return None
        ports = sorted({int(ln.split()[1]) for ln in res[1].splitlines() if ln.lower().startswith("port ")})
        return ports or None

    def sshd_denies(self, user: str, host: str, addr: str, laddr: str, lport: int) -> bool | None:
        """sshd's own effective ``denyusers`` for a connection context (``sshd -T -C``; needs root). No sshd installed:
        nobody can log in over SSH, so denied."""
        sshd = next((b for b in _SSHD if os.path.exists(b)), None)
        if sshd is None:
            return True
        ctx = f"user={user},host={host},addr={addr},laddr={laddr},lport={lport}"
        res = self._run([sshd, "-T", "-C", ctx])
        if res is None or res[0] != 0:
            return None
        pats = [p for ln in res[1].splitlines() if ln.lower().startswith("denyusers ") for p in ln.split()[1:]]
        return any(fnmatch(user, p) for p in pats)

    def pin(self, uid: int) -> dict | None:
        from ..host.pins import AgentPins

        return AgentPins(self._pin_dir).get(uid)

    def _show(self, unit: str) -> tuple[str, int]:
        import subprocess

        binary = next((b for b in _SYSTEMCTL if os.path.exists(b)), _SYSTEMCTL[0])
        out = subprocess.run(  # fixed absolute binary, no shell, fixed arguments
            [binary, "show", "-p", "ActiveState", "-p", "MainPID", "--", unit],
            capture_output=True,
            text=True,
            timeout=5,
            check=True,
            env={},
        ).stdout
        vals = dict(line.split("=", 1) for line in out.splitlines() if "=" in line)
        return vals["ActiveState"], int(vals["MainPID"])

    @staticmethod
    def _in_unit_cgroup(pid: int, unit: str) -> bool:
        """The unified-hierarchy line ``0::<path>`` must contain the unit as a whole path segment."""
        with open(f"/proc/{pid}/cgroup", encoding="utf-8") as handle:
            for line in handle.read().splitlines():
                if line.startswith("0::") and unit in line[3:].split("/"):
                    return True
        return False

    @staticmethod
    def _proc_status(pid: int) -> dict[str, str]:
        with open(f"/proc/{pid}/status", encoding="utf-8") as handle:
            return dict(ln.split(":", 1) for ln in handle.read().splitlines() if ":" in ln)

    def _identity(self, unit: str, pid: int) -> dict:
        """Who the main process really is, from /proc/<pid>/status; raises on anything inconsistent."""
        if not self._in_unit_cgroup(pid, unit):
            raise ValueError("pid is not in the unit's cgroup")
        status = self._proc_status(pid)
        uids = {int(x) for x in status["Uid"].split()}
        if len(uids) != 1:
            raise ValueError("real/effective/saved/fs uids differ")  # not a plain service identity
        gids = {int(x) for x in status["Gid"].split()} | {int(x) for x in status.get("Groups", "").split()}
        cap = {k: int(status[f"Cap{k}"].strip(), 16) for k in ("Eff", "Amb", "Prm", "Inh", "Bnd")}
        return {
            "uid": next(iter(uids)),
            "gids": frozenset(gids),
            "no_new_privs": status["NoNewPrivs"].strip() == "1",
            "cap_eff": cap["Eff"],
            "cap_amb": cap["Amb"],
            "cap_prm": cap["Prm"],
            "cap_inh": cap["Inh"],
            "cap_bnd": cap["Bnd"],
        }

    def unit_state(self, unit: str) -> UnitState:
        """Inactive (clearly down), transitional (restarting; with the identity of its process if one runs), or active
        with the identity of its main process; anything unclear is ``unknown`` (never a guess)."""
        import subprocess

        try:
            state, pid = self._show(unit)
            if state in ("inactive", "failed") and pid == 0:
                return UnitState("inactive")
            transitional = state in ("activating", "deactivating", "reloading")
            if transitional and pid == 0:
                return UnitState("transitional")  # no process runs: nothing to be exposed by
            if (state != "active" and not transitional) or pid <= 0:
                return UnitState("unknown")
            try:
                ident = self._identity(unit, pid)
            except FileNotFoundError:
                if transitional:
                    return UnitState("transitional")  # the process ended between the two reads (a crash loop)
                return UnitState("unknown")
            if self._show(unit) != (state, pid):
                return UnitState("unknown")  # changed while we looked (PID reuse, restart)
            return UnitState("transitional" if transitional else "active", **ident)
        except (OSError, ValueError, KeyError, subprocess.SubprocessError):
            return UnitState("unknown")
