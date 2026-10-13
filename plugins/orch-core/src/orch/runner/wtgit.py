"""Worktree git in the runner (H11d, addon #308, D-P2-24, D-P2-32, §12.7).

The host never runs git; the runner runs the two worktree commands the host sends (``git {op:"worktree.add" |
"worktree.remove", repo_pin, path, branch, base, force, generation}``), as the person (tier A), each as a supervised
child in its own process group with a 30 s limit that kills the group (only that group: the runner's own child).

**What git is allowed to run.** ``-c core.hooksPath=/dev/null`` (no hook, so a planted ``post-checkout`` never runs),
``-c core.fsmonitor=false`` (no fsmonitor daemon or hook), ``-c protocol.allow=never`` and an empty
``GIT_ALLOW_PROTOCOL`` (no network), no terminal prompt, no pager, no editor, and an environment built from scratch
(``HOME``, ``PATH``, ``LANG=C``, ``TMPDIR``): no ``GIT_DIR``, ``GIT_WORK_TREE``, ``GIT_CONFIG_*`` or other ``GIT_*``
variable of the runner reaches it. Filters (``.gitattributes`` only selects a driver; its command comes from the
person's global or system config, e.g. LFS) still run, as the person, exactly as when an agent runs git (§12.7).

**Confinement (H11d).** The worktree path must be exactly ``<repo>.worktrees/<WS8>-<KEY>`` (strict name regex, no ``..``,
no NUL). The parent of the repo and ``<repo>.worktrees`` are opened from ``/`` one component at a time with
``O_NOFOLLOW|O_DIRECTORY`` (a symlink anywhere is ``launch.cwd_changed``), the target is looked at with ``lstat`` through
that fd (a symlink or file is refused, never removed), git's cwd is that verified directory after its device and inode
were compared with the held fd, and the same check repeats after git returns. Residual: between the last check and git's
own ``chdir``/``mkdir`` a writer of the repo's parent directory could still swap names; that writer is the person's own
account, and the post-check detects the swap.

**The repository.** The declared repo's working copy is opened component-wise with ``O_NOFOLLOW`` and compared with the
host's pin (device and inode) before git starts; a mismatch is ``launch.cwd_changed``. git runs with ``-C`` set to
that path; the worktree path, the branch and the base come from the host (host-chosen path, a checked branch name, a
commit id), each after ``--`` where git takes one.
"""

from __future__ import annotations

import contextlib
import os
import re
import signal
import stat
import subprocess
from collections.abc import Callable, Mapping, Sequence
from typing import Any

from .supervise import SpawnError

__all__ = ["GIT_SECONDS", "git_argv", "git_env", "read_git", "run_git", "tree_dirty"]

GIT_SECONDS = 30.0
_SHA = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
#: the host-chosen directory name: ``<WS8>-<KEY>`` (8 hex of the workspace id, a ticket key); nothing else is accepted
_WT_NAME = re.compile(r"^[0-9a-f]{8}-[A-Z][A-Z0-9]{0,15}-[0-9]{1,6}$")
_BRANCH = re.compile(r"^[A-Za-z0-9._/][A-Za-z0-9._/@+-]{0,199}$")
NEUTRAL = ["-c", "core.hooksPath=/dev/null", "-c", "core.fsmonitor=false", "-c", "protocol.allow=never"]


def git_env(own: Mapping[str, str]) -> dict[str, str]:
    """The child's whole environment: built from scratch (never the runner's ``GIT_*``)."""
    env = {k: own[k] for k in ("HOME", "PATH", "TMPDIR", "USER", "LOGNAME") if k in own}
    env.setdefault("PATH", "/usr/bin:/bin:/usr/local/bin")
    env.update(
        LANG="C", LC_ALL="C", GIT_ALLOW_PROTOCOL="", GIT_TERMINAL_PROMPT="0", GIT_PAGER="cat", GIT_EDITOR="true",
        GIT_OPTIONAL_LOCKS="0",
    )  # fmt: skip
    return env


def _str(args: Mapping[str, Any], key: str) -> str:
    v = args.get(key)
    if not isinstance(v, str) or not v or len(v) > 1024 or "\0" in v:
        raise SpawnError("invalid.input", f"git: invalid {key}")
    return v


def _clean_abs(path: str) -> str:
    parts = path.split("/")
    if not path.startswith("/") or any(p in ("", ".", "..") for p in parts[1:]):
        raise SpawnError("invalid.input", "git: a path must be absolute and clean")
    return path


def git_argv(args: Mapping[str, Any], repo: str, *, present: bool | None = None) -> list[str]:
    """The git argv of one command (validated: defence in depth; the host computed every value). ``present`` is
    whether the remove target exists, as :func:`_confine` saw it through directory fds (default: ``lexists``)."""
    op = args.get("op")
    path = _clean_abs(_str(args, "path"))
    base = ["git", *NEUTRAL, "-C", repo, "worktree"]
    if op == "worktree.add":
        branch = _str(args, "branch")
        if not _BRANCH.fullmatch(branch) or branch.startswith("-") or ".." in branch:
            raise SpawnError("invalid.input", "git: invalid branch")
        start = args.get("base")
        if start is None:
            return [*base, "add", "--", path, branch]
        if not isinstance(start, str) or not _SHA.fullmatch(start):
            raise SpawnError("invalid.input", "git: base must be a commit id")
        return [*base, "add", "-b", branch, "--", path, start]
    if op == "worktree.remove":
        if not (os.path.lexists(path) if present is None else present):
            return [*base, "prune"]  # the directory is gone: only git's own record of it is left
        return [*base, "remove", *(["--force"] if args.get("force") is True else []), "--", path]
    raise SpawnError("invalid.input", "git: unknown op")


def _check_repo(pin: Any) -> str:
    """The declared repo's working copy, opened component-wise with ``O_NOFOLLOW`` and compared with the host's pin."""
    if not isinstance(pin, dict) or set(pin) != {"path", "dev", "ino"}:
        raise SpawnError("invalid.input", "git: invalid repo_pin")
    path = _clean_abs(_str(pin, "path"))
    from orch.gitread.errors import Unavailable
    from orch.gitread.fs import DirPin, open_pin

    try:
        d = open_pin(DirPin(path, int(pin["dev"]), int(pin["ino"])))
    except (Unavailable, OSError, TypeError, ValueError):
        raise SpawnError("launch.cwd_changed", "git: the repository is not the pinned one") from None
    d.close()
    return path


def _moved(what: str) -> SpawnError:
    return SpawnError("launch.cwd_changed", f"git: {what}")


class _Confined:
    """The verified ``<repo>.worktrees`` directory: an open fd (walked from ``/`` with ``O_NOFOLLOW``, every component
    a real directory), its identity, and whether the target exists. ``recheck()`` repeats the walk and compares."""

    def __init__(self, repo: str, path: str) -> None:
        self.repo, self.path = repo, path
        self.parent, self.base = os.path.dirname(repo), os.path.basename(repo) + ".worktrees"
        prefix = f"{repo}.worktrees/"
        name = path[len(prefix):] if path.startswith(prefix) else ""
        if not _WT_NAME.fullmatch(name) or ".." in name:
            raise SpawnError("invalid.input", "git: the worktree path is not <repo>.worktrees/<name>")
        self.name = name
        self.dir_path = f"{repo}.worktrees"
        self.fd = -1
        self.ident: tuple[int, int] = (0, 0)
        self.present = False

    def open(self, create: bool) -> None:
        from orch.gitread.errors import Unavailable
        from orch.gitread.fs import open_walk

        try:
            parent, _st = open_walk(self.parent)  # every component O_NOFOLLOW|O_DIRECTORY, from "/"
        except (Unavailable, OSError):
            raise _moved("a component of the repository's parent path is a symlink or not a directory") from None
        try:
            kind = parent.kind(self.base)
            if kind is None and create:
                try:
                    os.mkdir(self.base, 0o700, dir_fd=parent.fd)
                except FileExistsError:
                    pass
                except OSError:
                    raise _moved(f"{self.base} cannot be created") from None
                kind = parent.kind(self.base)
            if kind is None and not create:
                self.fd, self.present = -1, False
                return
            if kind != "dir":
                raise _moved(f"{self.base} is a symlink or not a directory")
            try:
                wt = parent.sub(self.base)
            except Unavailable:
                raise _moved(f"{self.base} cannot be opened without following a symlink") from None
        finally:
            parent.close()
        self.fd = wt.fd
        wt.fd = -1  # the fd now belongs to this object
        st = os.fstat(self.fd)
        self.ident = (st.st_dev, st.st_ino)
        try:
            tst = os.stat(self.name, dir_fd=self.fd, follow_symlinks=False)
        except FileNotFoundError:
            self.present = False
        else:
            if not stat.S_ISDIR(tst.st_mode):  # a symlink (or file) as the target: never followed, never removed
                raise _moved("the worktree target is a symlink or not a directory")
            self.present = True

    def check_cwd(self) -> None:
        """The path git will use as cwd resolves to the directory this object verified (device and inode)."""
        try:
            st = os.stat(self.dir_path, follow_symlinks=False)
        except OSError:
            raise _moved("the worktrees directory vanished") from None
        if stat.S_ISLNK(st.st_mode) or (st.st_dev, st.st_ino) != self.ident:
            raise _moved("the worktrees directory was replaced")

    def after(self, op: str) -> None:
        """After git: the chain is still the verified one and the target is a real directory (or gone, on remove)."""
        self.check_cwd()
        try:
            tst = os.stat(self.name, dir_fd=self.fd, follow_symlinks=False)
        except FileNotFoundError:
            if op == "worktree.add":
                raise _moved("the worktree is not where it was created") from None
            return
        if stat.S_ISLNK(tst.st_mode):
            raise _moved("the worktree target became a symlink")

    def close(self) -> None:
        if self.fd >= 0:
            with contextlib.suppress(OSError):
                os.close(self.fd)
            self.fd = -1


def _classify(op: str, stderr: str) -> str:
    low = stderr.lower()
    if op == "worktree.add" and ("is already used by worktree" in low or "is already checked out" in low):
        return "worktree.branch_in_use"
    if op == "worktree.add" and "already exists" in low and "branch" not in low:
        return "worktree.exists"
    if op == "worktree.remove" and ("modified or untracked" in low or "is dirty" in low):
        return "worktree.dirty"
    return "worktree.git_failed"


def run_git(
    args: Mapping[str, Any],
    own: Mapping[str, str],
    *,
    timeout: float = GIT_SECONDS,
    log: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """Run one worktree command. Returns ``{ok:true}``; raises :class:`SpawnError` with a ``worktree.*`` code (git's
    stderr goes only to ``log``, the runner's own log, never to the host)."""
    op = str(args.get("op"))
    repo = _check_repo(args.get("repo_pin"))
    if op not in ("worktree.add", "worktree.remove"):
        raise SpawnError("invalid.input", "git: unknown op")
    conf = _Confined(repo, _clean_abs(_str(args, "path")))
    try:
        conf.open(create=op == "worktree.add")
        if op == "worktree.add" and conf.present and os.listdir(conf.fd):
            raise SpawnError("worktree.exists", "git: the worktree path is not empty")
        argv = git_argv(args, repo, present=conf.present)
        if conf.fd >= 0:
            conf.check_cwd()
        out = _spawn(argv, own, timeout, log, op, cwd=conf.dir_path if conf.fd >= 0 else os.path.dirname(repo))
        if conf.fd >= 0:
            conf.after(op)
        return out
    finally:
        conf.close()


def _spawn(
    argv: list[str], own: Mapping[str, str], timeout: float, log: Callable[[str], None] | None, op: str, *, cwd: str
) -> dict[str, Any]:
    try:
        proc = subprocess.Popen(  # noqa: S603 - fixed argv, values checked above
            argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=git_env(own),
            cwd=cwd,  # the verified directory (the caller compared its device and inode with the fd it holds)
            start_new_session=True,  # its own process group: the timeout kills exactly that group
            close_fds=True,
        )
    except OSError as exc:
        raise SpawnError("worktree.git_failed", f"git could not start ({type(exc).__name__})") from None
    try:
        _out, err = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        with contextlib.suppress(OSError):
            os.killpg(proc.pid, signal.SIGKILL)  # the group this runner created for this command, nothing else
        with contextlib.suppress(Exception):
            proc.communicate(timeout=5)
        raise SpawnError("worktree.git_failed", f"git took longer than {timeout:g} s and was stopped") from None
    text = err.decode("utf-8", "replace")[-2000:]
    if proc.returncode != 0:
        if log is not None:
            log(f"git {op} failed ({proc.returncode}): {text}")
        code = _classify(op, text)
        raise SpawnError(code, f"git {op.split('.')[-1]} failed" + (" (exit %d)" % proc.returncode))
    return {"ok": True}


def read_git(repo: str, args: Sequence[str], own: Mapping[str, str], *, timeout: float = GIT_SECONDS) -> str | None:
    """A read-only git command for the direct-mode tool, hardened exactly as :func:`run_git` (the same ``NEUTRAL``
    config, the same from-scratch environment, no network, own process group, 30 s) and, since nothing here needs the
    person's filters, also with the system and global config off. Stdout stripped; ``None`` when git fails, times out
    or is missing. ``repo`` is a verified absolute path (``-C``)."""
    _clean_abs(repo)
    env = git_env(own)
    env.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL="/dev/null")
    try:
        proc = subprocess.Popen(  # noqa: S603 - fixed argv
            ["git", *NEUTRAL, "--no-optional-locks", "-C", repo, *args],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, env=env, cwd="/",
            start_new_session=True, close_fds=True,
        )  # fmt: skip
    except OSError:
        return None
    try:
        out, _ = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        with contextlib.suppress(OSError):
            os.killpg(proc.pid, signal.SIGKILL)
        with contextlib.suppress(Exception):
            proc.communicate(timeout=5)
        return None
    return out.decode("utf-8", "replace").strip() if proc.returncode == 0 else None


def tree_dirty(repo: str, own: Mapping[str, str]) -> str | None:
    """Uncommitted work in the working copy at ``repo`` (``""`` clean, lines when not, ``None`` when it cannot be told),
    through :func:`read_git`: ``status --porcelain`` plus the files git is told to ignore (``skip-worktree``,
    ``assume-unchanged``)."""
    status = read_git(repo, ["status", "--porcelain=v1", "--untracked-files=normal"], own)
    listing = read_git(repo, ["ls-files", "-v"], own)
    if status is None or listing is None:
        return None
    hidden = [ln[2:] for ln in listing.splitlines() if ln[:1] == "S" or ln[:1].islower()]
    return status + ("\n" if status and hidden else "") + "\n".join(f"hidden change flag: {h}" for h in hidden)
