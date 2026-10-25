"""Person-side sharing for tier B (#1198 H1 L5, spec orch-v2-host §13.2 item 6).

The agent runs as its own user (``orch-agent-<person>``). It can reach only what the PERSON shares with it, and the
person does that here, as themselves, on files they own (no root, no ``setfacl`` process): ACLs written with
``setxattr system.posix_acl_*`` on descriptors opened fd-relative with ``O_NOFOLLOW``.

* an ancestor of a shared path gets ``u:<agent>:--x`` (traverse, no listing);
* a repository's folder gets ``--x`` only (the working tree stays private); ``<repo>/.git`` and the release trees get
  ``r-x`` (``r--`` on files without the owner's execute bit) and, on directories, the same as a default ACL, so what
  git and the build create later is readable too;
* every ACL is read back and compared; a difference, a symlinked component, a foreign owner, an object that is not
  ours to change, or a change that would widen the access of anyone else (the group, other, any other named entry) is a
  refusal, never a guess. Nothing for group or other is ever added: those entries are copied from what is there, the
  mask is the smallest that keeps every other entry's effective permission as it was.

Tier A (no ``orch-agent-<person>`` user, or not Linux) is a no-op that says so. Re-applying changes nothing that is
already right. :func:`unshare` takes the agent's entries away again (``keep`` names the paths that still need their
ancestors).
"""

from __future__ import annotations

import errno
import os
import pwd
import stat
import struct
import sys
from collections.abc import Iterable
from dataclasses import dataclass, field

from .osapply import GROUP_OBJ, MASK, OTHER, USER, USER_OBJ, acl
from .osusers import AGENT_PREFIX, valid_person

__all__ = ["Report", "ShareError", "agent_for", "share_release", "share_repo", "unshare"]

GROUP = 0x08
_UNDEF = 0xFFFFFFFF
_ACCESS, _DEFAULT = "system.posix_acl_access", "system.posix_acl_default"
_TRAVERSE, _READ_EXEC, _READ = 0o1, 0o5, 0o4
_MAX_ENTRIES = 500_000  # objects in one shared tree; more is refused (a release or .git never gets near)
_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC


class ShareError(Exception):
    """A refusal with a plain sentence for the person. Nothing further is changed after one."""


@dataclass(frozen=True)
class Agent:
    name: str
    uid: int


@dataclass
class Item:
    path: str
    what: str  # "traverse" | "folder" | "tree" | "file"
    action: str  # "granted" | "unchanged" | "removed" | "skipped"
    detail: str = ""


@dataclass
class Report:
    noop: str | None = None  # why nothing was done (tier A)
    items: list[Item] = field(default_factory=list)
    problem: str | None = None  # the refusal; the items before it were applied (re-run to finish)

    @property
    def ok(self) -> bool:
        return self.problem is None

    def counts(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for it in self.items:
            out[it.action] = out.get(it.action, 0) + 1
        return out

    def text(self) -> str:
        if self.noop:
            return self.noop
        lines = [f"{it.action:9} {it.what:8} {it.path}" + (f" ({it.detail})" if it.detail else "") for it in self.items
                 if it.what in ("traverse", "folder", "tree") or it.action == "skipped"]  # fmt: skip
        total = self.counts()
        lines.append("Summary: " + (", ".join(f"{n} {k}" for k, n in sorted(total.items())) or "nothing to do"))
        if self.problem:
            lines.append(f"STOPPED: {self.problem}")
        return "\n".join(lines)


# --- the xattr seam (tests replace these three with an in-memory kernel) ---------------------------------------------


def _getx(fd: int, name: str) -> bytes | None:
    if not hasattr(os, "getxattr"):
        raise OSError(errno.ENOTSUP, "no extended attributes on this system")
    try:
        return os.getxattr(fd, name)
    except OSError as exc:
        if exc.errno in (errno.ENODATA, getattr(errno, "ENOATTR", errno.ENODATA)):
            return None
        raise


def _setx(fd: int, name: str, value: bytes) -> None:
    if not hasattr(os, "setxattr"):
        raise OSError(errno.ENOTSUP, "no extended attributes on this system")
    os.setxattr(fd, name, value)


def _removex(fd: int, name: str) -> None:
    if not hasattr(os, "removexattr"):
        raise OSError(errno.ENOTSUP, "no extended attributes on this system")
    try:
        os.removexattr(fd, name)
    except OSError as exc:
        if exc.errno not in (errno.ENODATA, getattr(errno, "ENOATTR", errno.ENODATA)):
            raise


# --- ACL values ------------------------------------------------------------------------------------------------

Entry = tuple[int, int, int]  # (tag, perm, id)


def _parse(blob: bytes) -> list[Entry]:
    if len(blob) < 4 or (len(blob) - 4) % 8 or struct.unpack_from("<I", blob)[0] != 2:
        raise ShareError("an ACL on the path is in a form orch does not recognise; nothing was changed there")
    out: list[Entry] = []
    for off in range(4, len(blob), 8):
        tag, perm, ident = struct.unpack_from("<HHI", blob, off)
        if tag not in (USER_OBJ, USER, GROUP_OBJ, GROUP, MASK, OTHER) or perm & ~0o7:
            raise ShareError("an ACL on the path has an entry orch does not recognise; nothing was changed there")
        out.append((tag, perm, ident if tag in (USER, GROUP) else _UNDEF))
    tags = [t for t, _, _ in out]
    if any(tags.count(t) != 1 for t in (USER_OBJ, GROUP_OBJ, OTHER)) or tags.count(MASK) > 1:
        raise ShareError("an ACL on the path is incomplete; nothing was changed there")
    if (USER in tags or GROUP in tags) and MASK not in tags:
        raise ShareError("an ACL on the path has named entries but no mask; nothing was changed there")
    return out


def _canon(entries: Iterable[Entry]) -> list[Entry]:
    return sorted(entries, key=lambda e: (e[0], e[2]))  # the kernel's order: by tag, then id


def _build(entries: Iterable[Entry]) -> bytes:
    return acl(tuple(_canon(entries)))


def _effective(entries: list[Entry]) -> dict[tuple[int, int], int]:
    mask = next((p for t, p, _ in entries if t == MASK), 0o7)
    return {(t, i): (p if t in (USER_OBJ, OTHER) else p & mask) for t, p, i in entries if t != MASK}


def _base_access(mode: int) -> list[Entry]:
    return [(USER_OBJ, (mode >> 6) & 7, _UNDEF), (GROUP_OBJ, (mode >> 3) & 7, _UNDEF), (OTHER, mode & 7, _UNDEF)]


def _base_default(mode: int) -> list[Entry]:
    """The default ACL's own entries when there is none yet. A default ACL replaces the umask for new objects, so the
    group and other entries copy the folder's bits minus write (what umask 022 gives): nothing new is ever writable."""
    return [(USER_OBJ, 0o7, _UNDEF), (GROUP_OBJ, (mode >> 3) & 0o5, _UNDEF), (OTHER, mode & 0o5, _UNDEF)]


def _with_agent(entries: list[Entry], uid: int, perm: int | None) -> list[Entry]:
    """``entries`` without the agent's entry, plus it at ``perm`` (None: removed), mask = smallest that covers every
    named entry and the group; no named entry left and no mask before: no mask."""
    rest = [e for e in entries if not (e[0] == USER and e[2] == uid) and e[0] != MASK]
    if perm is not None:
        rest.append((USER, perm, uid))
    named = [e for e in rest if e[0] in (USER, GROUP)]
    if named:
        cover = next(p for t, p, _ in rest if t == GROUP_OBJ)
        for _, p, _ in named:
            cover |= p
        rest.append((MASK, cover, _UNDEF))
    return _canon(rest)


def _check_not_wider(before: list[Entry], after: list[Entry], uid: int) -> None:
    old, new = _effective(before), _effective(after)
    for key, perm in new.items():
        if key == (USER, uid):
            continue
        if perm & ~old.get(key, 0):
            raise ShareError("sharing would widen the access of someone else on this path; nothing was changed there")
    for key, perm in old.items():
        if key not in new and perm and key != (USER, uid):
            raise ShareError("sharing would drop an entry of someone else on this path; nothing was changed there")


# --- one object -------------------------------------------------------------------------------------------------------


def _original_mode(fd: int, st: os.stat_result) -> int:
    """The nine permission bits as they were before sharing: from the access ACL's own entries when there is one
    (the group bits of the mode then show the mask), else from the mode."""
    try:
        blob = _getx(fd, _ACCESS)
    except OSError:
        return st.st_mode & 0o777
    if blob is None:
        return st.st_mode & 0o777
    e = {t: p for t, p, _ in _parse(blob) if t in (USER_OBJ, GROUP_OBJ, OTHER)}
    return (e[USER_OBJ] << 6) | (e[GROUP_OBJ] << 3) | e[OTHER]


def _set_entry(fd: int, kind: str, agent: Agent, perm: int | None, mode: int, where: str) -> str:
    """Make the agent's entry on ``fd`` exactly ``perm`` (None: absent), verify it, return the action."""
    name = _ACCESS if kind == "access" else _DEFAULT
    try:
        have = _getx(fd, name)
    except OSError as exc:
        raise ShareError(_fs_message(where, exc)) from None
    if have is None:
        if perm is None:
            return "unchanged"
        before = _base_access(mode) if kind == "access" else _base_default(mode)
    else:
        before = _parse(have)
    want = _with_agent(before, agent.uid, perm)
    if have is not None and _canon(before) == want:
        return "unchanged"
    _check_not_wider(before, want, agent.uid)
    named = [e for e in want if e[0] in (USER, GROUP)]
    try:
        if not named and kind == "default":
            _removex(fd, name)
        else:
            _setx(fd, name, _build(want))
    except OSError as exc:
        raise ShareError(_fs_message(where, exc)) from None
    try:
        _verify(fd, name, kind, want, named, before, agent, where)
    except ShareError:
        _restore(fd, name, have)
        raise
    return "granted" if perm is not None else "removed"


def _verify(fd, name, kind, want, named, before, agent, where) -> None:
    try:
        got = _getx(fd, name)
    except OSError as exc:
        raise ShareError(_fs_message(where, exc)) from None
    if named:
        if got is None or _canon(_parse(got)) != want:
            raise ShareError(f"{where}: the ACL read back differs from what was set; it was put back")
        return
    # no named entry left: the access ACL is now the plain mode (the kernel drops the xattr), a default ACL is gone
    if got is not None and any(t in (USER, GROUP) for t, _, _ in _parse(got)):
        raise ShareError(f"{where}: an ACL entry is still there after removing it; it was put back")
    if kind == "access":
        now = os.fstat(fd).st_mode
        eff_now = {(t, i): p for (t, i), p in _effective(_base_access(now)).items()}
        eff_before = {k: p for k, p in _effective(before).items() if k[0] != USER}
        if eff_now != eff_before:
            raise ShareError(f"{where}: the permissions changed when the entry was removed; it was put back")


def _restore(fd: int, name: str, have: bytes | None) -> None:
    try:
        if have is None:
            _removex(fd, name)
        else:
            _setx(fd, name, have)
    except OSError:
        pass  # the refusal that follows is the message; a failed restore only leaves the (narrow) entry


def _fs_message(where: str, exc: OSError) -> str:
    if exc.errno in (errno.ENOTSUP, errno.EOPNOTSUPP, errno.ENOSYS):
        return f"{where}: this file system does not support ACLs (mount it with ACLs on); nothing was changed there"
    return f"{where}: the ACL could not be read or set ({exc.strerror or exc}); nothing was changed there"


# --- paths ------------------------------------------------------------------------------------------------------------


def _plain(path: object) -> str:
    if not isinstance(path, str) or not path.startswith("/") or len(path) > 1024 or path == "/":
        raise ShareError("Write the full path, starting with /.")
    parts = path.split("/")[1:]
    if any(not p or p in (".", "..") for p in parts) or any(ord(c) < 32 or ord(c) == 127 for c in path):
        raise ShareError("Write the path without '.', '..', doubled or trailing slashes or control characters.")
    return path


def _inside(path: str, root: str) -> bool:
    return path == root or path.startswith(root.rstrip("/") + "/")


def _open_dir(name: str, dir_fd: int, where: str) -> int:
    try:
        return os.open(name, _DIR_FLAGS, dir_fd=dir_fd)
    except OSError as exc:
        if exc.errno == errno.EACCES and hasattr(os, "O_PATH"):  # a root folder without read: only fstat and traverse
            try:
                return os.open(name, os.O_PATH | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=dir_fd)
            except OSError:
                pass
        if exc.errno in (errno.ELOOP, errno.ENOTDIR):
            raise ShareError(f"{where} is a symbolic link or not a folder; orch does not follow it") from None
        if exc.errno == errno.ENOENT:
            raise ShareError(f"{where} does not exist") from None
        raise ShareError(f"{where} cannot be opened safely ({exc.strerror})") from None


class _Chain:
    """The descriptors from ``/`` down to ``path``, each opened from the previous with O_NOFOLLOW."""

    def __init__(self, path: str) -> None:
        self.path = _plain(path)
        self.names = self.path.split("/")[1:]
        self.fds: list[int] = []
        self.stats: list[os.stat_result] = []
        try:
            self.fds.append(os.open("/", _DIR_FLAGS))
            for i, part in enumerate(self.names):
                self.fds.append(_open_dir(part, self.fds[-1], "/" + "/".join(self.names[: i + 1])))
            self.stats = [os.fstat(fd) for fd in self.fds]
        except BaseException:
            self.close()
            raise

    def close(self) -> None:
        for fd in self.fds:
            with _quiet():
                os.close(fd)
        self.fds = []

    def at(self, i: int) -> str:
        return "/" + "/".join(self.names[:i])


class _quiet:
    def __enter__(self) -> None:
        return None

    def __exit__(self, *exc) -> bool:
        return True


def _traverse(
    chain: _Chain, agent: Agent, euid: int, report: Report, *, upto: int, remove: bool, keep: set[str]
) -> None:
    """``--x`` for the agent on the components 0..upto of the chain that are ours; the others must already be
    traversable by everyone (we cannot and must not change them)."""
    for i in range(upto + 1):
        st, where = chain.stats[i], chain.at(i) or "/"
        if not stat.S_ISDIR(st.st_mode):
            raise ShareError(f"{where} is not a folder")
        if st.st_uid != euid:
            if remove:
                continue
            if not st.st_mode & 0o001:
                raise ShareError(
                    f"{where} belongs to someone else and is not open to traverse; "
                    "the administrator has to allow it, orch cannot"
                )
            continue
        if remove and where in keep:
            report.items.append(Item(where, "traverse", "skipped", "still needed by another shared path"))
            continue
        act = _set_entry(
            chain.fds[i], "access", agent, None if remove else _TRAVERSE, _original_mode(chain.fds[i], st), where
        )
        report.items.append(Item(where, "traverse", act))


# --- trees ------------------------------------------------------------------------------------------------------------


def _grant_tree(root_fd: int, root_path: str, agent: Agent, euid: int, report: Report, *, remove: bool) -> None:
    """The agent's entry on the folder ``root_fd`` and, below it, on every folder (access + default ACL) and every
    regular file (access ACL), without following a link and staying on the same file system."""
    st0 = os.fstat(root_fd)
    seen = [0]

    def visit(fd: int, path: str, st: os.stat_result) -> None:
        seen[0] += 1
        if seen[0] > _MAX_ENTRIES:
            raise ShareError(f"{root_path} holds more than {_MAX_ENTRIES} entries; orch will not share a tree this big")
        if st.st_uid != euid:
            raise ShareError(f"{path} belongs to someone else; orch only shares what you own")
        perm = None if remove else _READ_EXEC
        mode = _original_mode(fd, st)
        acts = {_set_entry(fd, "access", agent, perm, mode, path), _set_entry(fd, "default", agent, perm, mode, path)}
        if path == root_path:
            report.items.append(Item(path, "tree", next(iter(acts - {"unchanged"}), "unchanged")))
        with os.scandir(fd) as it:
            kids = [(e.name, e.is_symlink()) for e in it]
        for name, is_link in sorted(kids):
            child = f"{path}/{name}"
            if is_link:
                continue  # a link carries no ACL and is never followed
            try:
                kst = os.stat(name, dir_fd=fd, follow_symlinks=False)
            except OSError as exc:
                raise ShareError(f"{child} cannot be read ({exc.strerror})") from None
            if kst.st_dev != st0.st_dev:
                report.items.append(Item(child, "file", "skipped", "another file system"))
                continue
            if stat.S_ISDIR(kst.st_mode):
                cfd = _open_dir(name, fd, child)
                try:
                    if (os.fstat(cfd).st_ino, os.fstat(cfd).st_dev) != (kst.st_ino, kst.st_dev):
                        raise ShareError(f"{child} changed while it was being shared; run it again")
                    visit(cfd, child, os.fstat(cfd))
                finally:
                    os.close(cfd)
            elif stat.S_ISREG(kst.st_mode):
                if kst.st_nlink > 1:
                    report.items.append(Item(child, "file", "skipped", "has another hard link"))
                    continue
                _file(fd, name, child, kst, agent, euid, remove)
            # sockets, devices and pipes carry nothing to share

    def _file(dfd: int, name: str, child: str, kst: os.stat_result, agent: Agent, euid: int, remove: bool) -> None:
        seen[0] += 1
        if seen[0] > _MAX_ENTRIES:
            raise ShareError(f"{root_path} holds more than {_MAX_ENTRIES} entries; orch will not share a tree this big")
        if kst.st_uid != euid:
            raise ShareError(f"{child} belongs to someone else; orch only shares what you own")
        try:
            ffd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, dir_fd=dfd)
        except OSError as exc:
            raise ShareError(f"{child} cannot be opened safely ({exc.strerror})") from None
        try:
            fst = os.fstat(ffd)
            if (fst.st_ino, fst.st_dev) != (kst.st_ino, kst.st_dev) or not stat.S_ISREG(fst.st_mode):
                raise ShareError(f"{child} changed while it was being shared; run it again")
            perm = None if remove else (_READ_EXEC if fst.st_mode & 0o100 else _READ)
            _set_entry(ffd, "access", agent, perm, _original_mode(ffd, fst), child)
        finally:
            os.close(ffd)

    visit(root_fd, root_path, st0)


# --- entry points -----------------------------------------------------------------------------------------------------


def agent_for(person: str | None = None) -> Agent | None:
    """The agent user of ``person`` (default: the user running this), or None where tier B does not exist."""
    if not sys.platform.startswith("linux"):
        return None
    name = person if person is not None else pwd.getpwuid(os.geteuid()).pw_name
    if not valid_person(name):
        return None
    try:
        entry = pwd.getpwnam(AGENT_PREFIX + name)
    except KeyError:
        return None
    return Agent(entry.pw_name, entry.pw_uid)


def _home_of_euid() -> str:
    return os.path.realpath(pwd.getpwuid(os.geteuid()).pw_dir)


def _preflight(agent: Agent | None, euid: int) -> Report | None:
    if agent is None:
        return Report(noop="No separate agent user on this machine (tier A): nothing to share, nothing changed.")
    if euid == 0 or agent.uid in (0, euid):
        raise ShareError("Run this as your own user, not as root; the agent user must be a different, non-root user.")
    return None


def share_repo(
    repo: str, *, agent: Agent | None | str = "auto", euid: int | None = None, home: str | None = None
) -> Report:
    """Share a registered repository: ``--x`` on its ancestors and its folder, ``r-x`` + default on ``<repo>/.git``."""
    return _run(repo, ".git", agent, euid, home, remove=False, keep=(), roots=lambda h: (h, repo))


def share_release(
    release: str, *, agent: Agent | None | str = "auto", euid: int | None = None, home: str | None = None
) -> Report:
    """Share a release (or an interpreter) tree inside your home: ``--x`` on the ancestors, ``r-x`` + default below."""
    return _run(release, None, agent, euid, home, remove=False, keep=(), roots=lambda h: (h,))


def unshare(
    path: str,
    *,
    is_repo: bool,
    keep: Iterable[str] = (),
    agent: Agent | None | str = "auto",
    euid: int | None = None,
    home: str | None = None,
) -> Report:
    """Take the agent's entries away from ``path`` (``<repo>/.git`` for a repo). Ancestors listed in ``keep`` (or
    that contain a ``keep`` path) stay: other shared paths still need them."""
    return _run(
        path, ".git" if is_repo else None, agent, euid, home, remove=True, keep=tuple(keep),
        roots=lambda h: (h, path) if is_repo else (h,),
    )  # fmt: skip


def _run(path, sub, agent, euid, home, *, remove, keep, roots) -> Report:
    euid = os.geteuid() if euid is None else euid
    resolved = agent_for() if agent == "auto" else agent
    try:
        stop = _preflight(resolved, euid)
        if stop is not None:
            return stop
        path = _plain(path)
        home = home or _home_of_euid()
        if not any(_inside(path, r) for r in roots(home)):
            raise ShareError(f"{path} is outside your home folder and is not a registered repository")
        keep_dirs = _ancestors_of(keep)
    except ShareError as exc:
        return Report(problem=str(exc))
    report = Report()
    chain = None
    try:
        chain = _Chain(path)
        last = len(chain.names)
        if sub is None:
            _traverse(chain, resolved, euid, report, upto=last - 1, remove=remove, keep=keep_dirs)
            target_fd = chain.fds[last]
            if remove:
                _check_owner(chain.stats[last], euid, path)
            _grant_tree(target_fd, path, resolved, euid, report, remove=remove)
        else:  # a repo: the folder itself gets traverse only, its .git the tree
            _traverse(chain, resolved, euid, report, upto=last, remove=remove, keep=keep_dirs)
            git_path = f"{path}/{sub}"
            gfd = _open_dir(sub, chain.fds[last], git_path)
            try:
                _grant_tree(gfd, git_path, resolved, euid, report, remove=remove)
            finally:
                os.close(gfd)
    except ShareError as exc:
        report.problem = str(exc)
    except OSError as exc:
        report.problem = f"{path}: {exc.strerror or exc}; nothing further was changed"
    finally:
        if chain is not None:
            chain.close()
    return report


def _check_owner(st: os.stat_result, euid: int, path: str) -> None:
    if st.st_uid != euid:
        raise ShareError(f"{path} belongs to someone else; orch only shares what you own")


def _ancestors_of(paths: Iterable[str]) -> set[str]:
    out: set[str] = set()
    for p in paths:
        p = _plain(p)
        parts = p.split("/")[1:]
        out.update("/" + "/".join(parts[:i]) for i in range(1, len(parts) + 1))
    return out
