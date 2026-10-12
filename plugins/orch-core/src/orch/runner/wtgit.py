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
import subprocess
from collections.abc import Callable, Mapping
from typing import Any

from .supervise import SpawnError

__all__ = ["GIT_SECONDS", "git_argv", "git_env", "run_git"]

GIT_SECONDS = 30.0
_SHA = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
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


def git_argv(args: Mapping[str, Any], repo: str) -> list[str]:
    """The git argv of one command (validated: defence in depth; the host computed every value)."""
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
        if not os.path.lexists(path):
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
    argv = git_argv(args, repo)
    try:
        proc = subprocess.Popen(  # noqa: S603 - fixed argv, values checked above
            argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=git_env(own),
            cwd="/",
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
