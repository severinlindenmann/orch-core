"""Worktree git in the runner (H11d, addon #308, D-P2-24, D-P2-32, §12.7).

The host never runs git; the runner runs the two worktree commands the host sends (``git {op:"worktree.add" |
"worktree.remove", repo_pin, path, branch, base, force, generation}``), as the person (tier A), each as a supervised
child in its own process group with a 30 s limit that kills the group (only that group: the runner's own child).

**What git is allowed to run** (the invariant of :mod:`orch.gitsafe`: orch never runs git against a repository whose
local/worktree config or attributes could make git execute a program or read config from elsewhere). Before **every**
git command (:func:`orch.gitsafe.gate`, called by :func:`run_git`) the
repository's config files must be owned by the person and its own local and worktree configuration is listed and
refused (``observe.unavailable``) when any key is not on the allow-list of :func:`orch.gitsafe.exec_risk` (no filter,
driver, hook, include, alias, pager, editor, credential, ssh/proxy/askpass or gpg program, no unknown section); on
remove the worktree's own configuration is checked too. ``worktree remove`` always runs with ``--force``: the clean
check is orch's own, before the command, with the reader (no git process, #565), so git's own ``status`` never runs.
Defence in depth on every command: ``-c
core.hooksPath=/dev/null`` (no hook), ``-c core.fsmonitor=false``, ``-c protocol.allow=never`` and an empty
``GIT_ALLOW_PROTOCOL`` (no network), ``-c submodule.recurse=false``, no terminal prompt, no pager, no editor, and an
environment built from scratch (``HOME``, ``PATH``, ``LANG=C``, ``TMPDIR``): no ``GIT_DIR``, ``GIT_WORK_TREE``,
``GIT_CONFIG_*`` or other ``GIT_*`` variable of the runner reaches it. ``worktree add`` is ``--no-checkout`` (#584)
and :func:`orch.gitread.checkout.populate` writes the tree: the repo's config names no filter that runs; only the
no external process runs during it (a filter attribute, git-lfs included,
refuses the worktree). :func:`run_git` is the one documented exception to
:data:`orch.gitsafe.ISOLATION` (the site table of :mod:`orch.gitsafe`), with a race note at its spawn (#565). The
direct-mode tool's reads (revisions, ``HEAD``, cleanliness) use :data:`orch.gitread.local.READER`, never git.

**Confinement (H11d).** The worktree path must be exactly ``<repo>.worktrees/<WS8>-<KEY>`` (strict name regex, no
``..``, no NUL). The parent of the repo and ``<repo>.worktrees`` are opened from ``/`` one component at a time with
``O_NOFOLLOW|O_DIRECTORY`` (a symlink anywhere is ``launch.cwd_changed``); every directory from ``/`` on must be owned
by root or the effective UID and not group- or other-writable unless sticky (Opus #439 D2), and those two must be
owned by this process's effective UID and writable by no group or other (``launch.cwd_changed`` otherwise), so only
the person's own account can rename anything on the path. The target is looked at with ``lstat`` through that fd
(a symlink or file is refused, never removed). git gets absolute paths (``-C <repo>``, ``-- <path>``) and runs with
cwd ``/``: the walk from ``/`` is repeated right before git starts (pre-check) and after it returns (post-check),
each comparing device and inode with the held fd. Residual: between the pre-check and git's own ``mkdir``/``rmdir`` a
writer of a directory on the path could swap names; that writer can only be the person's own account (or root), and
the post-check detects (cannot undo) the swap.

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
from collections.abc import Callable, Mapping
from typing import Any

from orch import gitsafe

from .supervise import SpawnError

__all__ = ["GIT_SECONDS", "git_argv", "git_env", "run_git"]

GIT_SECONDS = 30.0
_SHA = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
#: the host-chosen directory name: ``<WS8>-<KEY>`` (8 hex of the workspace id, a ticket key); nothing else is accepted
_WT_NAME = re.compile(r"^[0-9a-f]{8}-[A-Z][A-Z0-9]{0,15}-[0-9]{1,6}$")
_BRANCH = re.compile(r"^[A-Za-z0-9._/][A-Za-z0-9._/@+-]{0,199}$")
NEUTRAL = list(gitsafe.NEUTRAL)


def git_env(own: Mapping[str, str]) -> dict[str, str]:
    """The environment of :func:`run_git`'s worktree commands: built from scratch (never the runner's ``GIT_*``), with
    the person's system and global configuration **on**: the documented exception to
    :data:`orch.gitsafe.ISOLATION` (their checkout filters must run). Read-only commands use
    :func:`orch.gitsafe.isolated_env` instead."""
    env = {k: own[k] for k in ("HOME", "PATH", "TMPDIR", "USER", "LOGNAME") if k in own}
    env.setdefault("PATH", "/usr/bin:/bin:/usr/local/bin")
    env.update(
        LANG="C", LC_ALL="C", GIT_ALLOW_PROTOCOL="", GIT_TERMINAL_PROMPT="0", GIT_PAGER="cat", GIT_EDITOR="true",
        GIT_OPTIONAL_LOCKS="0", GIT_NO_LAZY_FETCH="1",
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
            return [*base, "add", "--no-checkout", "--", path, branch]
        if not isinstance(start, str) or not _SHA.fullmatch(start):
            raise SpawnError("invalid.input", "git: base must be a commit id")
        return [*base, "add", "--no-checkout", "-b", branch, "--", path, start]
    if op == "worktree.remove":
        if not (os.path.lexists(path) if present is None else present):
            return [*base, "prune"]  # the directory is gone: only git's own record of it is left
        # ALWAYS --force (#565): without it git runs its own `status` in the worktree to check that it is clean, and
        # that status runs any clean filter the repo's (or the worktree's) config names, read at that moment: a writer
        # of that config racing the gate could make it run a program. The clean check is orch's own instead, done
        # before this command with the reader (no git process): the host's tree.check child, `orch worktree remove`'s
        # LocalGitReader; only a person's explicit force skips it. `args["force"]` only records that choice.
        return [*base, "remove", "--force", "--", path]
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
        name = path[len(prefix) :] if path.startswith(prefix) else ""
        if not _WT_NAME.fullmatch(name) or ".." in name:
            raise SpawnError("invalid.input", "git: the worktree path is not <repo>.worktrees/<name>")
        self.name = name
        self.dir_path = f"{repo}.worktrees"
        self.fd = -1
        self.ident: tuple[int, int] = (0, 0)
        self.present = False

    def open(self, create: bool) -> None:
        from orch.gitread.errors import Unavailable

        parent, pst = _walk_safe(self.parent)  # every component O_NOFOLLOW|O_DIRECTORY, from "/", each checked
        try:
            _owned(pst, "the repository's parent directory")
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
        _owned(st, self.base)
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
        """The path git is given still resolves, walked from ``/`` with ``O_NOFOLLOW`` per component, to the directory
        this object verified (device and inode). Not a cwd pin (git gets absolute paths): the pre- and post-check."""
        d, st = _walk_safe(self.dir_path)
        d.close()
        if (st.st_dev, st.st_ino) != self.ident:
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


def _walk_safe(path: str) -> tuple[Any, os.stat_result]:
    """``path`` walked from ``/`` one component at a time with ``O_NOFOLLOW|O_DIRECTORY`` (a symlink anywhere is
    ``launch.cwd_changed``), and **every** directory on the way, ``/`` included, owned by root or this process's
    effective UID and not writable by group or other unless sticky (Opus #439 D2): no other account can rename a
    component of the path between the checks and git. Returns the open :class:`orch.gitread.fs.Dir` and its stat."""
    from orch.gitread.errors import Unavailable
    from orch.gitread.fs import Dir, abs_parts

    try:
        parts = abs_parts(path)
        cur = Dir(os.open("/", os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0)), "/")
    except (Unavailable, OSError):
        raise _moved("the path cannot be walked from /") from None
    try:
        st = os.fstat(cur.fd)
        _ancestor(st, "/")
        for comp in parts:
            try:
                nxt = cur.sub(comp)
            except (Unavailable, OSError):
                raise _moved("a component of the path is a symlink, not a directory, or gone") from None
            cur.close()
            cur = nxt
            st = os.fstat(cur.fd)
            _ancestor(st, cur.path)
    except BaseException:
        cur.close()
        raise
    return cur, st


def _ancestor(st: os.stat_result, what: str) -> None:
    if st.st_uid not in (0, os.geteuid()):
        raise _moved(f"{what} is owned by another user")
    if stat.S_IMODE(st.st_mode) & 0o022 and not st.st_mode & stat.S_ISVTX:
        raise _moved(f"{what} is writable by group or others and not sticky")


def _owned(st: os.stat_result, what: str) -> None:
    """Only the person's own account may rename entries in ``what`` (Opus #439 F2): owned by the effective UID, not
    writable by group or other."""
    if st.st_uid != os.geteuid() or stat.S_IMODE(st.st_mode) & 0o022:
        raise _moved(f"{what} is not owned by this user or is writable by others")


def _gated(repo: str, own: Mapping[str, str], what: str = "the repository") -> None:
    """The invariant (:mod:`orch.gitsafe`), checked right before git runs against ``repo``."""
    try:
        gitsafe.gate(repo, own)
    except gitsafe.Unsafe as exc:
        raise SpawnError("observe.unavailable", f"git: {what}: {exc.reason}"[:300]) from None


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
        _gated(repo, own)
        if op == "worktree.remove" and conf.present:  # remove reads the worktree's config: gated too (#565)
            _gated(conf.path, own, "the worktree")
        if conf.fd >= 0:
            conf.check_cwd()
        # The one exception to orch.gitsafe.ISOLATION ("no git invocation by orch reads configuration the person or a
        # repo controls, except through an explicit allow-list"), listed in its site table: the repo's own config is
        # gated above, but the person's system and global config stays on (git_env) by design. A worktree add or
        # remove is a checkout in the person's own repo, as the person: it must run their checkout filters (the smudge
        # of `git lfs install`) exactly as `git worktree add` in their terminal would.
        #
        # RACE NOTE (#565): the gate above checked the config; git reads it again when it starts, so a concurrent
        # writer of the repo's .git/config (or the worktree's config, or a swapped .git file) could change it in
        # between. Why that is accepted here, per command:
        # * remove: always `--force` (git_argv), after orch's own clean check with the reader (no git process), so
        #   git's own `status` never runs and no clean filter is read; `remove --force` checks out nothing.
        #   The cost: git's own second clean check at remove time is gone (it was that same filter-running status);
        #   a file written between orch's check and this remove is deleted with the worktree (data, not code).
        # * prune: reads only <repo>/.git/worktrees; no checkout, no status, no filter.
        # * add: `--no-checkout` (#584): git checks nothing out, so no smudge/process filter and no hook can run,
        #   whatever the repo's config says when git reads it. The tree is then written by orch's own writer
        #   (orch.gitread.checkout.populate): NO external process runs there at all. A path whose attributes name any
        #   filter (git-lfs too) refuses the whole worktree (worktree.checkout_unsupported); anything else it cannot
        #   write exactly fails closed. On any failure _undo_add removes the tree and the new branch completely.
        out = _spawn(argv, own, timeout, log, op)
        if conf.fd >= 0:
            conf.after(op)
        if op == "worktree.add":
            _populate(args, repo, conf, own, timeout, log)
        return out
    finally:
        conf.close()


def _populate(
    args: Mapping[str, Any],
    repo: str,
    conf: _Confined,
    own: Mapping[str, str],
    timeout: float,
    log: Callable[[str], None] | None,
) -> None:
    """Fill the ``--no-checkout`` worktree (#584). Any failure at all removes it again (and the branch ``-b`` created)
    and is ``worktree.checkout_unsupported``: nothing half-populated is left behind."""
    from orch.gitread.checkout import populate
    from orch.gitread.errors import Unavailable

    try:
        populate(conf.path, own, log=log)
    except BaseException as exc:
        reason = exc.reason if isinstance(exc, Unavailable) else f"unexpected {type(exc).__name__}"
        if log is not None:
            log(f"worktree.add checkout failed: {reason}")
        left = _undo_add(args, repo, conf, own, timeout, log)
        if not isinstance(exc, Exception):
            raise
        suffix = "" if left is None else f" (and it could not be fully removed: {left})"
        raise SpawnError(
            "worktree.checkout_unsupported", f"git: the worktree could not be populated: {reason}{suffix}"[:300]
        ) from None
    conf.after("worktree.add")


def _rmtree_at(dirfd: int, name: str) -> None:
    """Delete ``name`` below ``dirfd`` without following any symlink (fd-based, no path re-resolution)."""
    st = os.stat(name, dir_fd=dirfd, follow_symlinks=False)
    if not stat.S_ISDIR(st.st_mode):
        os.unlink(name, dir_fd=dirfd)
        return
    fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=dirfd)
    try:
        os.fchmod(fd, 0o700)
        for child in os.listdir(fd):
            _rmtree_at(fd, child)
    finally:
        os.close(fd)
    os.rmdir(name, dir_fd=dirfd)


def _undo_add(
    args: Mapping[str, Any],
    repo: str,
    conf: _Confined,
    own: Mapping[str, str],
    timeout: float,
    log: Callable[[str], None] | None,
) -> str | None:
    """Remove a half-populated worktree completely: ``worktree remove --force`` (checks nothing out, runs no filter);
    if anything of it is left, delete the tree directly (fd-based, only below the verified ``<repo>.worktrees``
    directory, no symlink followed) and ``worktree prune``; then the branch this add created (``-b``). Returns a
    reason when something could not be removed, else ``None``."""
    problem: str | None = None
    try:
        _spawn(git_argv({"op": "worktree.remove", "path": conf.path}, repo, present=True), own, timeout, log, "w")
    except SpawnError as exc:
        if log is not None:
            log(f"worktree.add cleanup: git remove failed ({exc.code}); deleting the tree directly")
    try:
        if conf.fd >= 0:
            conf.check_cwd()
            if os.path.lexists(conf.path) or _present_at(conf):
                _rmtree_at(conf.fd, conf.name)
            _spawn(["git", *NEUTRAL, "-C", repo, "worktree", "prune"], own, timeout, log, "worktree.prune")
    except (OSError, SpawnError) as exc:
        problem = type(exc).__name__
        if log is not None:
            log(f"worktree.add cleanup: direct removal failed ({problem})")
    if args.get("base") is not None:
        try:
            _spawn(
                ["git", *NEUTRAL, "-C", repo, "branch", "-D", "--", str(args.get("branch"))],
                own,
                timeout,
                log,
                "branch.delete",
            )
        except SpawnError as exc:
            problem = problem or "the new branch remains"
            if log is not None:
                log(f"worktree.add cleanup: branch delete failed ({exc.code})")
    return problem


def _present_at(conf: _Confined) -> bool:
    try:
        os.stat(conf.name, dir_fd=conf.fd, follow_symlinks=False)
    except FileNotFoundError:
        return False
    return True


def _spawn(
    argv: list[str], own: Mapping[str, str], timeout: float, log: Callable[[str], None] | None, op: str
) -> dict[str, Any]:
    try:
        proc = subprocess.Popen(  # noqa: S603 - fixed argv, values checked above
            argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=git_env(own),
            cwd="/",  # git gets absolute paths only; the caller ran the gate and the pre-check
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
        raise SpawnError(code, f"git {op.split('.')[-1]} failed" + f" (exit {proc.returncode})")
    return {"ok": True}
