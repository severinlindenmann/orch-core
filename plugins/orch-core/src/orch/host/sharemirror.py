"""What the person shares with the agent user: bare mirrors, the release and the interpreter (#1198 H1 L5, spec 13.2
item 5/6, design-h1-share-mirror).

**Invariant.** The agent user never gets an ACL entry in the person's home and never reaches a working tree. All it can
read is ``/var/lib/orch/share/<person>/``, and only the person writes there. Root made that folder (0700, one ACL entry
``u:<agent>:--x``, ``osapply.share_acl``); everything below is made here, as the person:

* ``mirrors/<ws8>/<name>.git``: a bare copy of the repository's **branches and tags** (never ``refs/stash``: ``git stash
  -u`` stores untracked files such as ``.env``). Created at registration, refreshed by :func:`sync_mirror` before an
  agent starts and on ``mirror.sync``. Its ``config`` is written byte for byte by :func:`config_bytes`: no remote, no
  credential setting, ``hooks/`` empty. git runs only here on the host, under :mod:`orch.gitsafe`'s gate and isolated
  environment, so nothing of the person's git configuration and nothing of the source repository's configuration (it
  must pass the allow-list) reaches the command.
* ``releases/<version>`` with ``current``, and ``python/`` (``UV_PYTHON_INSTALL_DIR``): the agent's runner starts
  from ``releases/current/bin/orch-runner``. The person owns them, so an update needs no root.

Every directory the agent must read (``mirrors``, each ``<ws8>``, ``releases``, ``python``) carries the ACL
``u:<agent>:r-x`` plus the same as a default, so what git and the release build create later is readable too (the
default has no group or other entry). Each ACL is read back and compared with what was set. All of it is
file-descriptor-relative with ``O_NOFOLLOW`` from the share folder; a symlink, a folder of someone else, or an ACL that
reads back differently is a refusal, never a guess.

Tier A (no agent user, or not Linux) has no share: :func:`share_for` answers ``None`` and nothing here runs.
"""

from __future__ import annotations

import contextlib
import errno
import fcntl
import json
import os
import posixpath
import pwd
import re
import secrets
import signal
import stat
import struct
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .. import gitsafe
from ..setup.osapply import GROUP_OBJ, MASK, OTHER, USER, USER_OBJ, acl
from ..setup.osusers import AGENT_PREFIX, share_dir, valid_person
from .isolation import AVAILABILITY, EXPOSURE, Reason

__all__ = [
    "NOT_SHARED",
    "REVIEW_LINE",
    "MirrorResult",
    "Report",
    "Share",
    "ShareError",
    "SyncGate",
    "check",
    "config_bytes",
    "create_mirror",
    "delete_mirror",
    "find_interpreter",
    "install_interpreter",
    "install_release_copy",
    "repair",
    "share_for",
    "share_registered_repo",
    "status_text",
    "sync_mirror",
]

#: the one line the registration review shows (spec 13.2 item 5)
REVIEW_LINE = (
    "Your agents get a read-only copy of this repository's branches. Your working folder and uncommitted files "
    "stay private."
)
NOT_SHARED = "Agents run as you; nothing is shared."
FETCH_SECONDS = 300.0
SYNC_INTERVAL = 30.0  # column G: at most one mirror.sync per repository in this time (calls inside it are coalesced)
MAX_HEADS = 2000
_WALK_BUDGET = 20_000
_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
_ACCESS, _DEFAULT = "system.posix_acl_access", "system.posix_acl_default"
_WS8 = re.compile(r"^[0-9a-f]{8}$")
_NAME = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
_HEAD = re.compile(r"^ref: (refs/heads/[A-Za-z0-9._/@+-]{1,200})\n?$")


class ShareError(Exception):
    """A refusal with a plain sentence for the person. Nothing further is changed after one."""


@dataclass(frozen=True)
class Share:
    person: str
    root: str  # SHARE_ROOT/<person>
    state: str  # the person's state directory: the registry and the locks live below it
    agent_uid: int

    @property
    def mirrors(self) -> str:
        return f"{self.root}/mirrors"

    def mirror_path(self, ws8: str, name: str) -> str:
        return f"{self.root}/mirrors/{ws8}/{name}.git"

    @property
    def releases(self) -> str:
        return f"{self.root}/releases"

    @property
    def python(self) -> str:
        return f"{self.root}/python"


@dataclass(frozen=True)
class MirrorResult:
    heads: dict[str, str]  # refs/heads/* and refs/tags/* → object id
    created: bool = False
    truncated: bool = False


def share_for(person: str, state: str | os.PathLike[str], *, platform: str | None = None) -> Share | None:
    """The person's share, or ``None`` in tier A (not Linux, an unusable name, or no ``orch-agent-<person>`` user)."""
    if (platform or sys.platform) != "linux" or not valid_person(person):
        return None
    try:
        agent = pwd.getpwnam(AGENT_PREFIX + person)
    except KeyError:
        return None
    return Share(person, share_dir(person), os.fspath(state), agent.pw_uid)


# --- the xattr seam (tests replace these two with an in-memory kernel) -----------------------------------------------


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


def rx_acl(agent_uid: int) -> bytes:
    """``u::rwx,u:<agent>:r-x,g::---,m::r-x,o::---``: the ACL of every folder the agent reads (access and default)."""
    return acl(((USER_OBJ, 7), (USER, 5, agent_uid), (GROUP_OBJ, 0), (MASK, 5), (OTHER, 0)))


def _fs_message(where: str, exc: OSError) -> str:
    if exc.errno in (errno.ENOTSUP, errno.EOPNOTSUPP, errno.ENOSYS):
        return f"{where}: this file system does not support ACLs (mount it with ACLs on); nothing was shared"
    return f"{where}: the ACL could not be read or set ({exc.strerror or exc}); nothing was shared"


def _set_exact(fd: int, name: str, want: bytes, where: str) -> None:
    """Make the ACL ``name`` of the directory ``fd`` exactly ``want``, then read it back."""
    try:
        if _getx(fd, name) == want:
            return
        _setx(fd, name, want)
        got = _getx(fd, name)
    except OSError as exc:
        raise ShareError(_fs_message(where, exc)) from None
    if got != want:
        raise ShareError(f"{where}: the ACL read back differs from what was set; nothing was shared")


# --- descriptors --------------------------------------------------------------------------------------------


def _mine(fd: int, where: str) -> None:
    if os.fstat(fd).st_uid != os.geteuid():
        raise ShareError(f"{where} belongs to someone else; orch will not use it")


def _open_chain(path: str) -> int:
    """``path`` (absolute) opened from ``/`` one component at a time with O_NOFOLLOW: no symlink on the way."""
    fd = os.open("/", _DIR_FLAGS)
    try:
        for part in (p for p in path.split("/") if p):
            nxt = os.open(part, _DIR_FLAGS, dir_fd=fd)
            os.close(fd)
            fd = nxt
        return fd
    except BaseException:
        os.close(fd)
        raise


def _open_share(share: Share) -> int:
    try:
        fd = _open_chain(share.root)
    except FileNotFoundError:
        raise ShareError(
            "The read-only place for your agents is not there yet. Run the admin step (orch-os-setup) again."
        ) from None
    except OSError as exc:
        raise ShareError(f"{share.root} cannot be opened safely ({exc.strerror}); orch will not use it") from None
    try:
        _mine(fd, share.root)
    except BaseException:
        os.close(fd)
        raise
    return fd


def _child(parent: int, name: str, share: Share, where: str) -> int:
    """The directory ``name`` below ``parent``: opened without following links, made (0700) when missing, owned by
    the person, with the agent's ``r-x`` access and default ACL."""
    try:
        fd = os.open(name, _DIR_FLAGS, dir_fd=parent)
    except FileNotFoundError:
        try:
            os.mkdir(name, 0o700, dir_fd=parent)
            fd = os.open(name, _DIR_FLAGS, dir_fd=parent)
        except OSError as exc:
            raise ShareError(f"{where} could not be made ({exc.strerror})") from None
    except OSError as exc:
        raise ShareError(f"{where} is a link or not a folder ({exc.strerror}); orch will not follow it") from None
    try:
        _mine(fd, where)
        want = rx_acl(share.agent_uid)
        _set_exact(fd, _ACCESS, want, where)
        _set_exact(fd, _DEFAULT, want, where)
    except BaseException:
        os.close(fd)
        raise
    return fd


def _clear(fd: int) -> None:
    with os.scandir(fd) as it:
        entries = [(e.name, e.is_dir(follow_symlinks=False)) for e in it]
    for name, is_dir in entries:
        if is_dir:
            sub = os.open(name, _DIR_FLAGS, dir_fd=fd)
            try:
                os.fchmod(sub, 0o700)
                _clear(sub)
            finally:
                os.close(sub)
            os.rmdir(name, dir_fd=fd)
        else:
            try:
                os.unlink(name, dir_fd=fd)
            except PermissionError:
                os.fchmod(fd, 0o700)
                os.unlink(name, dir_fd=fd)


def _rmtree_at(parent: int, name: str) -> None:
    """Remove ``name`` below ``parent`` without following a link at any level."""
    try:
        fd = os.open(name, _DIR_FLAGS, dir_fd=parent)
    except FileNotFoundError:
        return
    except OSError:  # a file or a link where a folder should be: remove just that entry
        with contextlib.suppress(OSError):
            os.unlink(name, dir_fd=parent)
        return
    try:
        _mine(fd, name)
        os.fchmod(fd, 0o700)
        _clear(fd)
    finally:
        os.close(fd)
    os.rmdir(name, dir_fd=parent)


def _slurp(path: str, limit: int = 1 << 20) -> bytes | None:
    """A regular file's bytes, read without following a link (``None``: absent, a link or not a regular file)."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK)
    except OSError:
        return None
    with os.fdopen(fd, "rb") as handle:
        if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
            return None
        return handle.read(limit)


# --- the registry and the locks (under the person's state directory) ----------------------------------------


def _state_dir(share: Share) -> Path:
    base = Path(share.state)
    for level in (base, base / "share", base / "share" / "locks"):
        level.mkdir(mode=0o700, exist_ok=True)
        info = os.lstat(level)
        if stat.S_ISLNK(info.st_mode) or info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) & 0o077:
            raise ShareError(f"{level} is not a private folder of yours; orch will not use it")
    return base / "share"


def _registry(share: Share) -> dict[str, dict[str, Any]]:
    path = _state_dir(share) / "mirrors.json"
    raw = _slurp(os.fspath(path))
    if raw is None:
        if os.path.lexists(path):
            raise ShareError(f"{path} is not a plain file; orch will not use it")
        return {}
    try:
        doc = json.loads(raw)
    except ValueError:
        raise ShareError(f"{path} is damaged; remove it and run orch share repair") from None
    mirrors = doc.get("mirrors") if isinstance(doc, dict) else None
    return mirrors if isinstance(mirrors, dict) else {}


def _save_registry(share: Share, mirrors: dict[str, dict[str, Any]]) -> None:
    path = _state_dir(share) / "mirrors.json"
    tmp = f"{path}.tmp-{secrets.token_hex(4)}"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(json.dumps({"v": 1, "mirrors": mirrors}, sort_keys=True, indent=1) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


@contextlib.contextmanager
def _locked(share: Share, key: str) -> Iterator[None]:
    """The per-mirror lock (git must not run twice at once on one mirror)."""
    path = _state_dir(share) / "locks" / (key.replace("/", "-") + ".lock")
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)  # closing releases the lock


# --- git ----------------------------------------------------------------------------------------------------


def config_bytes(mirror: str) -> bytes:
    """The mirror's ``config``, exactly: no remote, no credential or URL setting, hooks at its empty ``hooks/``."""
    return (
        "[core]\n\trepositoryformatversion = 0\n\tbare = true\n\tlogAllRefUpdates = false\n"
        f"\thooksPath = {mirror}/hooks\n[fetch]\n\tfsckObjects = true\n[gc]\n\tautoDetach = false\n"
    ).encode()


_GIT_PINS = (
    "-c", "protocol.file.allow=always", "-c", "fetch.fsckObjects=true", "-c", "transfer.fsckObjects=true",
    "-c", "receive.fsckObjects=true", "-c", "gc.autoDetach=false", "-c", "init.defaultBranch=main",
)  # fmt: skip


def _git(args: Sequence[str], *, mirror: str | None, timeout: float = FETCH_SECONDS) -> tuple[int, str, str]:
    """One git command under gitsafe's hardening and from-scratch environment (only the ``file`` protocol allowed),
    in its own process group, killed on timeout. Returns ``(returncode, stdout, stderr)``."""
    argv = [gitsafe._git_exe(), *gitsafe.HARDEN, *_GIT_PINS, "--no-pager", "--no-optional-locks"]  # noqa: SLF001
    if mirror is not None:
        argv += ["-C", mirror]
    argv += list(args)
    env = gitsafe.isolated_env(os.environ)
    env["GIT_ALLOW_PROTOCOL"] = "file"
    try:
        proc = subprocess.Popen(  # noqa: S603 - fixed argv, no shell
            argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env, cwd="/",
            start_new_session=True, close_fds=True,
        )  # fmt: skip
    except OSError as exc:
        raise ShareError(f"git cannot be started ({exc.strerror})") from None
    try:
        out, err = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        with contextlib.suppress(OSError):
            os.killpg(proc.pid, signal.SIGKILL)
        with contextlib.suppress(Exception):
            proc.communicate(timeout=5)
        raise ShareError("git took too long to copy the repository; nothing was changed") from None
    return proc.returncode, out.decode("utf-8", "replace"), err.decode("utf-8", "replace")[-2000:]


def _source(repo: str) -> str:
    """The source repository's ``.git`` directory, after the gate: orch does not run git against a repository whose own
    configuration could make git run a program."""
    if not isinstance(repo, str) or not repo.startswith("/") or "\0" in repo or posixpath.normpath(repo) != repo:
        raise ShareError("The repository path must be a plain absolute path.")
    gitdir = f"{repo}/.git"
    try:
        info = os.lstat(gitdir)
    except OSError:
        raise ShareError(f"{repo} is not a repository with its own .git folder.") from None
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid():
        raise ShareError(f"{repo}/.git is not a folder of yours; orch will not copy from it.")
    try:
        gitsafe.gate(repo, os.environ)
    except gitsafe.Unsafe as exc:
        raise ShareError(str(exc)) from None
    return gitdir


def _fetch(mirror: str, gitdir: str) -> None:
    code, _out, err = _git(
        ["fetch", "--prune", "--no-tags", "--no-write-fetch-head", gitdir,
         "+refs/heads/*:refs/heads/*", "+refs/tags/*:refs/tags/*"],
        mirror=mirror,
    )  # fmt: skip
    if code != 0:
        if re.search(r"fsck|bad object|broken|corrupt|invalid|hash mismatch", err, re.I):
            raise ShareError(
                "A branch of this repository did not pass git's integrity check, so the shared copy was not updated."
            )
        raise ShareError("The shared copy could not be updated from this repository.")


def _set_head(mirror: str, gitdir: str) -> None:
    """Point the mirror's HEAD at the branch the working repository has checked out (a clone then starts there)."""
    raw = _slurp(f"{gitdir}/HEAD", 512)
    m = _HEAD.match(raw.decode("utf-8", "replace")) if raw is not None else None
    if m is None:
        return
    from orch.gitread.refs import branch_ok

    ref = m.group(1)
    if branch_ok(ref.removeprefix("refs/heads/")):
        _git(["symbolic-ref", "HEAD", ref], mirror=mirror, timeout=30.0)


def _heads(mirror: str) -> tuple[dict[str, str], bool]:
    code, out, _err = _git(
        ["for-each-ref", "--format=%(objectname) %(refname)", "refs/heads", "refs/tags"], mirror=mirror, timeout=60.0
    )
    if code != 0:
        raise ShareError("The shared copy could not be read back.")
    heads: dict[str, str] = {}
    for line in out.splitlines():
        sha, _, ref = line.partition(" ")
        if sha and ref:
            heads[ref] = sha
    if len(heads) > MAX_HEADS:
        return dict(sorted(heads.items())[:MAX_HEADS]), True
    return heads, False


# --- create, sync, delete -----------------------------------------------------------------------------------


def _names(ws8: str, name: str) -> None:
    if not _WS8.match(ws8) or not _NAME.match(name):
        raise ShareError("That repository cannot be shared: its name is not usable.")


def _empty_template(share: Share) -> str:
    path = _state_dir(share) / "empty-template"
    path.mkdir(mode=0o700, exist_ok=True)
    if any(path.iterdir()):
        raise ShareError(f"{path} must be empty; orch will not use it")
    return os.fspath(path)


def share_registered_repo(share: Share | None, ws8: str, name: str, repo: str) -> MirrorResult | None:
    """The function the registration flow calls (tier B only; ``None`` in tier A, nothing is shared). Its review line
    is :data:`REVIEW_LINE`. Idempotent: a mirror that exists is refreshed."""
    if share is None:
        return None
    return create_mirror(share, ws8, name, repo)


def _record(share: Share, key: str, repo: str) -> None:
    reg = _registry(share)
    reg[key] = {"repo": repo, "synced": int(time.time())}
    _save_registry(share, reg)


def create_mirror(share: Share, ws8: str, name: str, repo: str) -> MirrorResult:
    _names(ws8, name)
    key = f"{ws8}/{name}"
    final = share.mirror_path(ws8, name)
    with _locked(share, key):
        if os.path.lexists(final):
            return _refresh(share, ws8, name, repo)
        gitdir = _source(repo)
        root = _open_share(share)
        try:
            top = _child(root, "mirrors", share, share.mirrors)
            try:
                parent = _child(top, ws8, share, f"{share.mirrors}/{ws8}")
            finally:
                os.close(top)
            try:
                tmp_name = f".{name}.tmp-{secrets.token_hex(4)}"
                tmp = f"{share.mirrors}/{ws8}/{tmp_name}"
                os.mkdir(tmp_name, 0o700, dir_fd=parent)
                try:
                    code, _o, _e = _git(
                        ["init", "--bare", f"--template={_empty_template(share)}", tmp], mirror=None, timeout=60.0
                    )
                    if code != 0:
                        raise ShareError("The shared copy could not be started.")
                    _write_config(parent, tmp_name, final)
                    _fetch(tmp, gitdir)
                    _set_head(tmp, gitdir)
                    _git(["gc", "--auto", "--quiet"], mirror=tmp)
                    os.rename(tmp_name, f"{name}.git", src_dir_fd=parent, dst_dir_fd=parent)
                except BaseException:
                    _rmtree_at(parent, tmp_name)
                    raise
            finally:
                os.close(parent)
        finally:
            os.close(root)
        _record(share, key, repo)
        heads, cut = _heads(final)
        return MirrorResult(heads, created=True, truncated=cut)


def _write_config(parent: int, tmp_name: str, final: str) -> None:
    """Replace git's own ``config`` with ours, created with O_EXCL; ``hooks/`` is made empty."""
    box = os.open(tmp_name, _DIR_FLAGS, dir_fd=parent)
    try:
        with contextlib.suppress(FileNotFoundError):
            os.unlink("config", dir_fd=box)
        fd = os.open("config", os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600, dir_fd=box)
        with os.fdopen(fd, "wb") as handle:
            handle.write(config_bytes(final))
            handle.flush()
            os.fsync(handle.fileno())
        with contextlib.suppress(FileExistsError):
            os.mkdir("hooks", 0o700, dir_fd=box)
        for stray in ("description", "FETCH_HEAD"):
            with contextlib.suppress(FileNotFoundError):
                os.unlink(stray, dir_fd=box)
    finally:
        os.close(box)


def _refresh(share: Share, ws8: str, name: str, repo: str) -> MirrorResult:
    """Fetch into the live mirror (the caller holds its lock). An integrity failure leaves the refs as they were: git
    updates none of them from a pack that does not pass."""
    mirror = share.mirror_path(ws8, name)
    gitdir = _source(repo)
    _fetch(mirror, gitdir)
    _set_head(mirror, gitdir)
    _git(["gc", "--auto", "--quiet"], mirror=mirror)
    _record(share, f"{ws8}/{name}", repo)
    heads, cut = _heads(mirror)
    return MirrorResult(heads, truncated=cut)


def sync_mirror(share: Share, ws8: str, name: str, repo: str | None = None) -> MirrorResult:
    """Bring the mirror up to date (before an agent starts or gets a worktree, and on ``mirror.sync``). ``repo`` is the
    working repository; omitted, the registry's record is used."""
    _names(ws8, name)
    key = f"{ws8}/{name}"
    with _locked(share, key):
        source = repo or (_registry(share).get(key) or {}).get("repo")
        if not isinstance(source, str) or not os.path.lexists(share.mirror_path(ws8, name)):
            raise ShareError("No shared copy of this repository exists yet.")
        return _refresh(share, ws8, name, source)


def delete_mirror(share: Share, ws8: str, name: str) -> bool:
    """Unregister: move the mirror to a trash name (atomic), then remove it. ``False``: there was none."""
    _names(ws8, name)
    key = f"{ws8}/{name}"
    gone = False
    with _locked(share, key):
        root = _open_share(share)
        try:
            top = os.open("mirrors", _DIR_FLAGS, dir_fd=root)
            try:
                parent = os.open(ws8, _DIR_FLAGS, dir_fd=top)
            finally:
                os.close(top)
        except OSError:
            parent = None
        finally:
            os.close(root)
        if parent is not None:
            try:
                trash = f".trash-{secrets.token_hex(4)}"
                try:
                    os.rename(f"{name}.git", trash, src_dir_fd=parent, dst_dir_fd=parent)
                    gone = True
                except FileNotFoundError:
                    pass
                if gone:
                    _rmtree_at(parent, trash)
            finally:
                os.close(parent)
        reg = _registry(share)
        if reg.pop(key, None) is not None:
            _save_registry(share, reg)
    return gone


# --- column G: at most one sync per repository per 30 s, coalesced ------------------------------------------


class _Flight:
    def __init__(self) -> None:
        self.done = threading.Event()
        self.answer: Any = None
        self.error: BaseException | None = None


class SyncGate:
    """A call that arrives while a sync of the same repository runs waits for it and gets its answer; a limited caller
    (column G) inside :data:`SYNC_INTERVAL` of the last finished sync gets that answer again. The person is never
    limited, but also joins a running sync."""

    def __init__(self, interval: float = SYNC_INTERVAL, clock: Callable[[], float] = time.monotonic) -> None:
        self.interval, self.clock = interval, clock
        self._mu = threading.Lock()
        self._running: dict[str, _Flight] = {}
        self._last: dict[str, tuple[float, Any]] = {}

    def run(self, key: str, work: Callable[[], Any], *, limited: bool) -> tuple[Any, bool]:
        """``(answer, ran)``: ``ran`` is False when the answer was shared with another call or is the last one."""
        with self._mu:
            flight = self._running.get(key)
            if flight is None:
                last = self._last.get(key)
                if limited and last is not None and self.clock() - last[0] < self.interval:
                    return last[1], False
                flight = self._running[key] = _Flight()
                mine = True
            else:
                mine = False
        if not mine:
            flight.done.wait()
            if flight.error is not None:
                raise flight.error
            return flight.answer, False
        try:
            flight.answer = work()
        except BaseException as exc:  # shared with the waiters, then raised for this call too
            flight.error = exc
            raise
        finally:
            with self._mu:
                self._running.pop(key, None)
                if flight.error is None:
                    self._last[key] = (self.clock(), flight.answer)
            flight.done.set()
        return flight.answer, True


# --- the release and the interpreter ------------------------------------------------------------------------


def find_interpreter(share: Share) -> str | None:
    """The interpreter ``uv python install`` put under ``<share>/python`` (``cpython-*/bin/python3``), or ``None``."""
    try:
        for entry in sorted(Path(share.python).iterdir()):
            exe = entry / "bin" / "python3"
            if entry.name.startswith("cpython-") and exe.is_file():
                return os.fspath(exe)
    except OSError:
        return None
    return None


def install_interpreter(
    share: Share, version: str | None = None, *, run: Callable[..., Any] = subprocess.run, uv: str = "uv"
) -> str:
    """``uv python install`` into ``<share>/python`` as the person (``UV_PYTHON_INSTALL_DIR``), then the interpreter."""
    version = version or f"{sys.version_info[0]}.{sys.version_info[1]}"
    if not re.fullmatch(r"3\.[0-9]{1,2}(\.[0-9]{1,2})?", version):
        raise ShareError("That Python version cannot be installed.")
    root = _open_share(share)
    try:
        os.close(_child(root, "python", share, share.python))
    finally:
        os.close(root)
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": os.environ.get("HOME", "/"),
        "UV_PYTHON_INSTALL_DIR": share.python,
    }
    try:
        done = run(
            [uv, "python", "install", version], env=env, capture_output=True, text=True, timeout=900, check=False
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise ShareError(f"uv could not be run ({exc})") from None
    exe = find_interpreter(share)
    if done.returncode != 0 or exe is None:
        raise ShareError("Python for your agents could not be installed.")
    return exe


def install_release_copy(share: Share, *, source: Path | None = None) -> Path:
    """The host's copy step after the admin step: this code (or ``source``) becomes ``<share>/releases/<version>`` with
    ``current``, its launchers naming the interpreter in ``<share>/python``. The agent reads it; nobody else writes."""
    from .release import ReleaseError, install_release

    interpreter = find_interpreter(share)
    if interpreter is None:
        raise ShareError("Python for your agents is not installed yet (orch share repair installs it).")
    root = _open_share(share)
    try:
        for sub in ("python", "releases"):  # both carry the agent's read ACL (the interpreter may be placed by hand)
            os.close(_child(root, sub, share, f"{share.root}/{sub}"))
    finally:
        os.close(root)
    try:
        return install_release(target=Path(share.releases), source=source, python=interpreter)
    except ReleaseError as exc:
        raise ShareError(str(exc)) from None


# --- the doctor ---------------------------------------------------------------------------------------------


def _acl_problem(fd: int, st: os.stat_result, agent_uid: int) -> str | None:
    """Why this object is open to someone besides the person and the agent's ``r-x``, else ``None``."""
    try:
        blobs = [_getx(fd, _ACCESS), _getx(fd, _DEFAULT)]
    except OSError:
        return "its ACL cannot be read"
    if st.st_mode & 0o007:
        return "other users can read it"
    for kind, blob in zip(("access", "default"), blobs, strict=True):
        if blob is None:
            if kind == "access" and st.st_mode & 0o070:
                return "its group can read it"
            continue
        if len(blob) < 4 or (len(blob) - 4) % 8 or struct.unpack_from("<I", blob)[0] != 2:
            return "it has an ACL orch cannot read"
        for off in range(4, len(blob), 8):
            tag, perm, ident = struct.unpack_from("<HHI", blob, off)
            if tag == USER and (ident != agent_uid or perm & ~0o5):
                return "it has an ACL entry besides the agent's read access"
            if tag in (GROUP_OBJ, OTHER) and perm:
                return "its group or others can read it"
            if tag not in (USER_OBJ, USER, GROUP_OBJ, MASK, OTHER):
                return "it has a group ACL entry"
    return None


Visit = Callable[[int | None, str, os.stat_result], None]


def _walk(fd: int, where: str, budget: list[int], visit: Visit) -> None:
    with os.scandir(fd) as it:
        entries = sorted(e.name for e in it)
    for name in entries:
        budget[0] -= 1
        if budget[0] < 0:
            raise ShareError(f"{where} holds too many entries to check")
        st = os.stat(name, dir_fd=fd, follow_symlinks=False)
        full = f"{where}/{name}"
        if stat.S_ISDIR(st.st_mode):
            sub = os.open(name, _DIR_FLAGS, dir_fd=fd)
            try:
                visit(sub, full, st)
                _walk(sub, full, budget, visit)
            finally:
                os.close(sub)
        elif stat.S_ISREG(st.st_mode):
            try:
                one = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK, dir_fd=fd)
            except OSError:
                visit(None, full, st)
                continue
            try:
                visit(one, full, st)
            finally:
                os.close(one)
        else:
            visit(None, full, st)  # a link or a special file


def _read_refs(gitdir: str) -> dict[str, str] | None:
    """Branch and tag heads of a git directory from its files (loose and packed), no git process."""
    refs: dict[str, str] = {}
    packed = _slurp(f"{gitdir}/packed-refs")
    for line in (packed or b"").decode("utf-8", "replace").splitlines():
        if line and line[0] not in "#^":
            sha, _, ref = line.partition(" ")
            refs[ref] = sha
    try:
        for kind in ("heads", "tags"):
            base = Path(gitdir, "refs", kind)
            for root, _dirs, files in os.walk(base):
                for f in files:
                    path = Path(root, f)
                    raw = _slurp(os.fspath(path), 256)
                    if raw is None:
                        return None
                    refs[f"refs/{kind}/{path.relative_to(base).as_posix()}"] = raw.decode("utf-8", "replace").strip()
    except OSError:
        return None
    return {k: v for k, v in refs.items() if k.startswith(("refs/heads/", "refs/tags/"))}


def check(share: Share) -> list[Reason]:
    """The doctor's facts for the share (design 6): an exposure when a mirror's config is not ours, its ``hooks/`` has
    a file, it holds a ref besides heads and tags, a FETCH_HEAD, a link, a foreign owner or an ACL beyond the agent's
    ``r-x``, or the release can be written by the agent, the group or others; availability when a mirror or the
    release is missing or a mirror is behind its repository (a stale mirror never pauses anything)."""
    try:
        root = _open_share(share)
    except (ShareError, OSError) as exc:
        return [Reason("share_missing", str(exc), AVAILABILITY)]
    out: list[Reason] = []
    try:
        for key, entry in sorted(_registry(share).items()):
            ws8, _, name = key.partition("/")
            out += _check_mirror(share, ws8, name, entry)
        out += _check_release(root, share.root)
    except (ShareError, OSError) as exc:
        out.append(Reason("share_unreadable", f"The shared folder could not be checked: {exc}", EXPOSURE))
    finally:
        os.close(root)
    return out


def _check_mirror(share: Share, ws8: str, name: str, entry: Mapping[str, Any]) -> list[Reason]:
    out: list[Reason] = []
    path = share.mirror_path(ws8, name)
    try:
        fd = _open_chain(path)
    except OSError:
        return [Reason("mirror_missing", f"The shared copy of {name} is missing.", AVAILABILITY)]
    try:
        if _slurp(f"{path}/config") != config_bytes(path):
            out.append(Reason("mirror_config", f"The shared copy of {name} has a configuration orch did not write."))
        hooks = Path(path, "hooks")
        if hooks.is_symlink() or not hooks.is_dir() or any(hooks.iterdir()):
            out.append(Reason("mirror_hooks", f"The shared copy of {name} has hooks."))
        if os.path.lexists(f"{path}/FETCH_HEAD"):
            out.append(Reason("mirror_fetch_head", f"The shared copy of {name} remembers where it was fetched from."))
        extra = sorted(p.name for p in Path(path, "refs").iterdir() if p.name not in ("heads", "tags"))
        for line in (_slurp(f"{path}/packed-refs") or b"").decode("utf-8", "replace").splitlines():
            ref = line.partition(" ")[2]
            if line and line[0] not in "#^" and not ref.startswith(("refs/heads/", "refs/tags/")):
                extra.append(ref)
        if extra:
            out.append(Reason("mirror_refs", f"The shared copy of {name} holds more than branches and tags."))
        problems: list[str] = []

        def visit(one: int | None, where: str, st: os.stat_result) -> None:
            if not stat.S_ISDIR(st.st_mode) and not stat.S_ISREG(st.st_mode):
                problems.append(f"{where}: a link or special file")
            elif st.st_uid != os.geteuid():
                problems.append(f"{where}: not yours")
            elif one is not None and (why := _acl_problem(one, st, share.agent_uid)):
                problems.append(f"{where}: {why}")

        visit(fd, name, os.fstat(fd))
        _walk(fd, name, [_WALK_BUDGET], visit)
        if problems:
            out.append(
                Reason("mirror_exposed", f"The shared copy of {name} is open to more than the agent: {problems[0]}.")
            )
    finally:
        os.close(fd)
    source = entry.get("repo")
    if isinstance(source, str) and not out:
        want, have = _read_refs(f"{source}/.git"), _read_refs(path)
        if want is not None and have is not None and want != have:
            out.append(Reason("mirror_stale", f"The shared copy of {name} is behind the repository.", AVAILABILITY))
    return out


def _check_release(root: int, share_root: str) -> list[Reason]:
    out: list[Reason] = []
    for sub in ("releases", "python"):
        try:
            fd = os.open(sub, _DIR_FLAGS, dir_fd=root)
        except FileNotFoundError:
            if sub == "releases":
                out.append(Reason("release_missing", "The program for your agents is not copied in yet.", AVAILABILITY))
            continue
        except OSError:
            out.append(Reason("release_exposed", f"{sub} in the shared folder is a link or not a folder."))
            continue
        if sub == "releases" and not os.path.lexists(f"{share_root}/releases/current"):
            out.append(Reason("release_missing", "The program for your agents is not copied in yet.", AVAILABILITY))
        bad: list[str] = []

        def visit(one: int | None, where: str, st: os.stat_result, bad: list[str] = bad) -> None:
            if stat.S_ISLNK(st.st_mode):
                return  # a link's own mode means nothing; where it points is judged by the target's own permissions
            if st.st_uid != os.geteuid() or st.st_mode & 0o022:
                bad.append(where)
                return
            if one is None:
                return
            try:
                blob = _getx(one, _ACCESS)
            except OSError:
                bad.append(where)
                return
            for off in range(4, len(blob or b""), 8):
                tag, perm, ident = struct.unpack_from("<HHI", blob, off)  # type: ignore[arg-type]
                if tag in (USER, 0x08) and perm & 0o2 and ident != os.geteuid():
                    bad.append(where)

        try:
            visit(fd, sub, os.fstat(fd))
            _walk(fd, sub, [_WALK_BUDGET], visit)
        finally:
            os.close(fd)
        if bad:
            out.append(Reason("release_writable", f"The program for your agents can be changed by others ({bad[0]})."))
    return out


# --- orch share status / repair -----------------------------------------------------------------------------


@dataclass
class Report:
    lines: list[str] = field(default_factory=list)
    ok: bool = True

    def text(self) -> str:
        return "\n".join(self.lines)


def status_text(share: Share | None) -> Report:
    if share is None:
        return Report([NOT_SHARED])
    try:
        reg = _registry(share)
    except ShareError as exc:
        return Report([f"Problem: {exc}"], ok=False)
    rep = Report([f"Shared with {AGENT_PREFIX}{share.person} (read-only): {len(reg)} repositories."])
    for key in sorted(reg):
        rep.lines.append(f"  {key}  {reg[key].get('repo', '?')}")
    found = check(share)
    for r in found:
        rep.lines.append(f"  {'PROBLEM' if r.kind == EXPOSURE else 'note'}: {r.message}")
    rep.ok = not any(r.kind == EXPOSURE for r in found)
    if not found:
        rep.lines.append("Everything is as orch set it up.")
    return rep


def repair(share: Share | None, *, run: Callable[..., Any] = subprocess.run) -> Report:
    """Make the share right again: the folders and their ACLs, every registered mirror (missing ones are made, the rest
    refreshed) and the release copy when it is missing. Never widens anything: a foreign owner or link is a refusal."""
    if share is None:
        return Report([NOT_SHARED])
    rep = Report()
    try:
        root = _open_share(share)
        try:
            for sub in ("mirrors", "releases", "python"):
                os.close(_child(root, sub, share, f"{share.root}/{sub}"))
        finally:
            os.close(root)
        for key, entry in sorted(_registry(share).items()):
            ws8, _, name = key.partition("/")
            repo = entry.get("repo")
            try:
                if not isinstance(repo, str):
                    raise ShareError("its repository is not recorded")
                got = create_mirror(share, ws8, name, repo)
                rep.lines.append(f"{'made' if got.created else 'refreshed'}  {key}")
            except ShareError as exc:
                rep.ok = False
                rep.lines.append(f"PROBLEM: {key}: {exc}")
        if not os.path.lexists(f"{share.releases}/current"):
            try:
                if find_interpreter(share) is None:
                    install_interpreter(share, run=run)
                    rep.lines.append("installed Python for your agents")
                rep.lines.append(f"copied the release: {install_release_copy(share)}")
            except ShareError as exc:
                rep.ok = False
                rep.lines.append(f"PROBLEM: the program for your agents: {exc}")
    except ShareError as exc:
        rep.ok = False
        rep.lines.append(f"PROBLEM: {exc}")
    if rep.ok and not rep.lines:
        rep.lines.append("Nothing to repair.")
    return rep
