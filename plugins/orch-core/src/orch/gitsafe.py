"""The one gate in front of every git command orch runs against a worktree repository (H11d, Opus #439 F1).

**Invariant.** orch never runs git against a repository whose local/worktree config or attributes could make git
execute a program or read config from elsewhere.

git takes programs from configuration: filter drivers (``filter.<x>.smudge|clean|process``), diff and merge drivers,
``core.fsmonitor``, ssh and proxy commands, credential helpers, pagers, editors, gpg. A repository's own
configuration (``.git/config``, ``config.worktree``) is as writable as its files: an unpacked archive, a copied
project or a shared volume brings one along. Patching one flag per program (``core.fsmonitor=false``,
``core.hooksPath=/dev/null``) failed three times, so the rule is enforced here, structurally:

* :func:`gate` lists the repository's **local and worktree** configuration with git itself (``git config
  --no-includes --list --show-scope -z``: reading configuration executes nothing) under a neutral environment
  (no system or global config, no ``GIT_*`` of the caller) and refuses (:class:`Unsafe`) when :func:`exec_risk` names
  any key. A repository whose configuration cannot be listed is refused too (fail closed).
* **Attributes** (``.gitattributes`` in any tree, ``$GIT_DIR/info/attributes``) only *select* a driver by name; the
  program always comes from configuration. Once the gate passed, the repository's own configuration defines no driver
  program, so an attribute can select only (a) a built-in behaviour (``text``, ``eol``, ``ident``,
  ``working-tree-encoding``: no program) or (b) a driver the person configured globally (e.g. ``filter=lfs`` from
  ``git lfs install``), which runs as the person exactly as when they run git themselves. ``core.attributesFile`` in
  the repository's configuration (attributes read from elsewhere) is refused. No attribute file is parsed.
* **Kept, defence in depth:** :data:`NEUTRAL` (``-c`` on every command: hooks to ``/dev/null``, fsmonitor off, no
  network protocol, no submodule recursion) and a from-scratch environment (the callers').

Two keys are deliberately **not** refused because a command-scope ``-c`` in :data:`NEUTRAL` overrides them on every
command and command scope always wins: ``core.hooksPath`` (husky and similar tools set it in the local config) and a
boolean ``core.fsmonitor`` (``true``/``false``: git's built-in daemon, no configured program). A non-boolean
``core.fsmonitor`` (a program path) is refused.

Used by :mod:`orch.runner.wtgit` (``run_git``, ``read_git``, ``tree_dirty``: the runner's ``git`` command and the
direct-mode ``orch worktree`` tool) and by direct-mode D58 on a registered worktree (:mod:`orch.ops.human`). The
records tool (:mod:`orch.records.git`) applies :func:`exec_risk` in its own, stricter ``check_local``.
"""

from __future__ import annotations

import contextlib
import os
import signal
import subprocess
from collections.abc import Iterable, Mapping

__all__ = ["INVARIANT", "NEUTRAL", "Unsafe", "exec_risk", "gate", "local_config"]

INVARIANT = (
    "orch never runs git against a repository whose local/worktree config or attributes could make git execute a "
    "program or read config from elsewhere"
)

#: command-scope overrides on every git command (defence in depth; command scope wins over every config file)
NEUTRAL = (
    "-c", "core.hooksPath=/dev/null",
    "-c", "core.fsmonitor=false",
    "-c", "protocol.allow=never",
    "-c", "submodule.recurse=false",
)  # fmt: skip

GATE_SECONDS = 10.0

#: whole sections the repository's own config may not use
_SECTIONS = frozenset({"include", "includeif", "credential"})
#: ``section.name`` (no subsection)
_KEYS = frozenset(
    {
        "core.attributesfile", "core.sshcommand", "core.gitproxy", "core.askpass", "core.pager", "core.editor",
        "core.alternaterefscommand", "sequence.editor", "gpg.program", "diff.external", "uploadpack.packobjectshook",
        "lfs.standalonetransferagent",
    }
)  # fmt: skip
#: ``section.<sub>.name``: (section, name)
_SUB_KEYS = frozenset(
    {
        ("filter", "smudge"), ("filter", "clean"), ("filter", "process"),
        ("diff", "textconv"), ("diff", "command"), ("merge", "driver"),
        ("gpg", "program"), ("remote", "uploadpack"), ("remote", "receivepack"),
        ("lfs", "path"),  # lfs.customtransfer.<name>.path (git-lfs runs it)
    }
)  # fmt: skip
#: ``section.<sub>.*`` (any name)
_SUB_SECTIONS = frozenset({"lfs.extension"})  # lfs.extension.<name>.{clean,smudge}: programs
_BOOL = frozenset({"true", "false", "yes", "no", "on", "off", "1", "0", ""})


class Unsafe(Exception):
    """The repository's own configuration could make git run a program (or cannot be checked): ``reason`` says which
    key (never its value)."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def _split(key: str) -> tuple[str, str | None, str]:
    section, _, rest = key.partition(".")
    sub, _, name = rest.rpartition(".")
    return section.lower(), (sub or None), name.lower()


def exec_risk(entries: Iterable[tuple[str, str, str]]) -> str | None:
    """The first local or worktree configuration key (``entries`` are ``(scope, key, value)``) that could make git
    execute a program or read configuration from elsewhere, or ``None``. Other scopes are ignored."""
    for scope, key, value in entries:
        if scope not in ("local", "worktree"):
            continue
        section, sub, name = _split(key)
        if section in _SECTIONS:
            return key
        if sub is None:
            if f"{section}.{name}" in _KEYS:
                return key
            if (section, name) == ("core", "fsmonitor") and value.strip().lower() not in _BOOL:
                return key
            continue
        if (section, name) in _SUB_KEYS:
            return key
        head = sub.split(".", 1)[0].lower()
        if f"{section}.{head}" in _SUB_SECTIONS:
            return key
    return None


def _parse(raw: bytes) -> list[tuple[str, str, str]]:
    """``git config --list --show-scope -z``: ``scope NUL key LF value NUL`` per entry."""
    parts = raw.split(b"\0")
    out: list[tuple[str, str, str]] = []
    i = 0
    while i + 1 < len(parts):
        scope = parts[i].decode("utf-8", "surrogateescape")
        key, _, value = parts[i + 1].decode("utf-8", "surrogateescape").partition("\n")
        out.append((scope, key, value))
        i += 2
    return out


def gate_env(own: Mapping[str, str]) -> dict[str, str]:
    env = {k: own[k] for k in ("HOME", "PATH", "TMPDIR", "USER", "LOGNAME") if k in own}
    env.setdefault("PATH", "/usr/bin:/bin:/usr/local/bin")
    env.update(LANG="C", LC_ALL="C", GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL="/dev/null", GIT_TERMINAL_PROMPT="0",
               GIT_OPTIONAL_LOCKS="0", GIT_ALLOW_PROTOCOL="")  # fmt: skip
    return env


def local_config(repo: str, own: Mapping[str, str], *, timeout: float = GATE_SECONDS) -> list[tuple[str, str, str]]:
    """The local and worktree configuration of the repository at ``repo`` (absolute), listed by git without
    following includes (an ``include.*`` key is listed, and refused, as itself). Raises :class:`Unsafe` when it
    cannot be listed."""
    if not repo.startswith("/") or "\0" in repo:
        raise Unsafe("the repository path is not absolute")
    try:
        proc = subprocess.Popen(  # noqa: S603 - fixed argv
            ["git", *NEUTRAL, "--no-optional-locks", "-C", repo, "config", "--no-includes", "--list", "--show-scope",
             "-z"],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, env=gate_env(own), cwd="/",
            start_new_session=True, close_fds=True,
        )  # fmt: skip
    except OSError:
        raise Unsafe("git cannot be started to check the repository's config") from None
    try:
        out, _ = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        with contextlib.suppress(OSError):
            os.killpg(proc.pid, signal.SIGKILL)  # the group this call created, nothing else
        with contextlib.suppress(Exception):
            proc.communicate(timeout=5)
        raise Unsafe("listing the repository's config timed out") from None
    if proc.returncode != 0:
        raise Unsafe("the repository's config cannot be listed")
    return [e for e in _parse(out) if e[0] in ("local", "worktree")]


def gate(repo: str, own: Mapping[str, str], *, timeout: float = GATE_SECONDS) -> None:
    """Refuse (:class:`Unsafe`) to let git near ``repo`` when its own configuration could make git execute a program
    or read configuration from elsewhere (:data:`INVARIANT`)."""
    key = exec_risk(local_config(repo, own, timeout=timeout))
    if key is not None:
        raise Unsafe(f"the repository's own config sets {key}, which can make git run a program; orch does not run "
                     "git there (remove it, or configure it globally)")  # fmt: skip
