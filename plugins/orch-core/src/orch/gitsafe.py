"""The one gate in front of every git command orch runs against a worktree repository (H11d, Opus #439 F1).

**Invariant.** orch never runs git against a repository whose local/worktree config or attributes could make git
execute a program or read config from elsewhere.

git takes programs from configuration: filter drivers, diff and merge drivers, ``core.fsmonitor``, config-based
hooks (``hook.<name>.command``, git 2.54, which ``-c core.hooksPath`` does **not** turn off), ssh and proxy commands,
credential helpers, pagers, editors, gpg, aliases. A repository's own configuration (``.git/config``,
``config.worktree``) is as writable as its files: an unpacked archive, a copied project or a shared volume brings one
along. A deny-list of such keys missed four times in one review cycle (hooks, fsmonitor, filters, config hooks), and
every git release can add a key, so the rule is an **allow-list** (owner decision, 13 Oct 2026, Opus #439 D1):

* :func:`gate` checks that the repository's config files (``<common dir>/config``, ``config.worktree``) and their
  git dirs are owned by the effective UID and not group- or other-writable (:func:`config_files`, Opus D3), then lists
  the **local and worktree** configuration with git itself (``git config --no-includes --list --show-scope -z``:
  reading configuration executes nothing) under a neutral environment (no system or global config, no ``GIT_*`` of
  the caller) and refuses (:class:`Unsafe`) the first key that is not on :data:`ALLOWED` / :data:`ALLOWED_SUB`
  (:func:`exec_risk`): exactly the keys an ordinary repository needs, none that names a program, a command, a
  driver, a hook or another config file. An unknown section is refused. A repository whose configuration cannot be
  listed is refused too (fail closed). :data:`KNOWN_EXEC` keeps the known exec keys as documentation and tests.
* **Submodules** (Codex #439): :func:`submodules` refuses a working copy with a populated submodule; its own config
  is a different repository's, and ``--ignore-submodules`` would hide its changes. Every cleanliness verdict calls it.
* **Attributes** (``.gitattributes`` in any tree, ``$GIT_DIR/info/attributes``) only *select* a driver by name; the
  program always comes from configuration. Once the gate passed, the repository's own configuration defines no driver
  program, so an attribute can select only (a) a built-in behaviour (``text``, ``eol``, ``ident``,
  ``working-tree-encoding``: no program) or (b) a driver the person configured globally (e.g. ``filter=lfs`` from
  ``git lfs install``), which runs as the person exactly as when they run git themselves. No attribute file is parsed.
* **Kept, defence in depth:** :data:`NEUTRAL` (``-c`` on every command: hooks to ``/dev/null``, fsmonitor off, no
  network protocol, no submodule recursion) and a from-scratch environment (the callers').

Two keys are on the allow-list only because a command-scope ``-c`` in :data:`NEUTRAL` overrides them on every command
and command scope always wins: ``core.hooksPath`` (husky and similar tools set it in the local config) and a boolean
``core.fsmonitor`` (``true``/``false``: git's built-in daemon, no configured program). A non-boolean
``core.fsmonitor`` (a program path) is refused.

Used by :mod:`orch.runner.wtgit` (``run_git``, ``read_git``, ``tree_dirty``: the runner's ``git`` command and the
direct-mode ``orch worktree`` tool) and by direct-mode D58 on a registered worktree (:mod:`orch.ops.human`). The
records tool (:mod:`orch.records.git`) applies :func:`exec_risk` (the same allow-list) first in its own, stricter
``check_local``.
"""

from __future__ import annotations

import contextlib
import os
import signal
import stat
import subprocess
from collections.abc import Iterable, Mapping

__all__ = [
    "ALLOWED", "ALLOWED_SUB", "INVARIANT", "KNOWN_EXEC", "NEUTRAL", "Unsafe", "config_files", "exec_risk", "gate",
    "local_config", "submodules",
]  # fmt: skip

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

#: Documentation only (the decision is :data:`ALLOWED`): keys known to make git execute a program or read config from
#: elsewhere. Four misses of this class in one review cycle (hooks, fsmonitor, filters, ``hook.<name>.command``) showed
#: that a deny-list falls behind every git release, so it is no longer the rule. The tests assert each is refused.
KNOWN_EXEC = (
    "hook.<name>.command", "hook.<name>.event", "core.hooksPath (overridden, see below)", "core.fsmonitor=<program>",
    "filter.<x>.{smudge,clean,process}", "diff.<x>.{textconv,command}", "diff.external", "merge.<x>.driver",
    "include.path", "includeIf.<cond>.path", "core.attributesFile", "core.sshCommand", "core.gitProxy", "core.askPass",
    "core.pager", "pager.<cmd>", "core.editor", "sequence.editor", "alias.<name>", "credential.*", "gpg.program",
    "gpg.<fmt>.program", "gpg.ssh.defaultKeyCommand", "remote.<n>.{uploadpack,receivepack,vcs}",
    "uploadpack.packObjectsHook", "gc.recentObjectsHook", "core.alternateRefsCommand",
    "{difftool,mergetool,man,browser,guitool}.<x>.{cmd,path}", "trailer.<x>.{cmd,command}", "sendemail.*Cmd",
    "web.browser", "help.browser", "lfs.customtransfer.<x>.path", "lfs.extension.<x>.*", "lfs.standalonetransferagent",
    "submodule.<n>.update=!<cmd>", "init.templateDir", "pull.twohead / pull.octopus (git-merge-<x> from PATH)",
    "branch.<n>.mergeOptions",
)  # fmt: skip

#: The ALLOW-LIST (Opus #439 D1, owner decision 13 Oct 2026): ``section -> names`` a repository's own (local or
#: worktree) configuration may set without a subsection; ``"*"`` allows every name of a section that only holds
#: booleans, colours or identities. Exactly what an ordinary repository needs (``git init``, ``git clone``, branch
#: tracking, per-repo identity and signing switches, maintenance tuning) and nothing that names a program, a command,
#: a driver, a hook or another config file. Anything else is refused, named by key (never by value).
ALLOWED: dict[str, frozenset[str] | str] = {
    "core": frozenset({
        "repositoryformatversion", "filemode", "bare", "logallrefupdates", "ignorecase", "precomposeunicode",
        "symlinks", "autocrlf", "eol", "safecrlf", "quotepath", "abbrev", "compression", "loosecompression",
        "bigfilethreshold", "sparsecheckout", "sparsecheckoutcone", "splitindex", "untrackedcache", "commitgraph",
        "multipackindex", "checkstat", "trustctime", "preloadindex", "fscache", "longpaths", "sharedrepository",
        "warnambiguousrefs", "protectntfs", "protecthfs", "checkroundtripencoding", "whitespace", "fsync",
        "fsyncmethod", "filesreflocktimeout", "packedrefstimeout", "deltabasecachelimit", "packedgitlimit",
        "packedgitwindowsize", "restrictinheritedhandles",
        "fsmonitor",  # boolean only (a program path is refused below); -c core.fsmonitor=false overrides it anyway
        "hookspath",  # -c core.hooksPath=/dev/null overrides it on every command (husky sets it locally)
    }),
    "remote": frozenset({"pushdefault"}),
    "branch": frozenset({"autosetupmerge", "autosetuprebase", "sort"}),
    "user": "*",
    "author": frozenset({"name", "email"}),
    "committer": frozenset({"name", "email"}),
    "init": frozenset({"defaultbranch"}),
    "extensions": frozenset({"objectformat", "worktreeconfig", "refstorage", "preciousobjects", "partialclone",
                             "noop"}),
    "gc": frozenset({
        "auto", "autodetach", "autopacklimit", "bigpackthreshold", "cruftpacks", "logexpiry", "packrefs",
        "pruneexpire", "reflogexpire", "reflogexpireunreachable", "rerereresolved", "rerereunresolved",
        "worktreepruneexpire", "writecommitgraph", "aggressivedepth", "aggressivewindow", "maxcruftsize",
    }),
    "pack": frozenset({
        "window", "depth", "windowmemory", "compression", "threads", "indexversion", "packsizelimit", "usebitmaps",
        "usesparse", "writebitmaps", "writebitmaphashcache", "writebitmaplookuptable", "writereverseindex",
        "deltacachesize", "deltacachelimit", "allowpackreuse", "usepathwalk",
    }),
    "fetch": frozenset({
        "prune", "prunetags", "recursesubmodules", "fsckobjects", "writecommitgraph", "showforcedupdates", "parallel",
        "negotiationalgorithm", "output", "all", "unpacklimit",
    }),
    "pull": frozenset({"rebase", "ff"}),
    "push": frozenset({"default", "followtags", "autosetupremote", "recursesubmodules", "gpgsign", "usebitmaps",
                       "negotiate"}),
    "merge": frozenset({
        "conflictstyle", "ff", "log", "renames", "renamelimit", "directoryrenames", "autostash", "stat",
        "verifysignatures", "defaulttoupstream", "branchdesc", "renormalize",
    }),
    "rebase": frozenset({
        "autosquash", "autostash", "stat", "updaterefs", "missingcommitscheck", "abbreviatecommands", "forkpoint",
        "backend", "rebasemerges", "instructionformat", "rescheduledfailedexec",
    }),
    "submodule": frozenset({"recurse", "active", "fetchjobs", "propagatebranches"}),
    "lfs": frozenset({"repositoryformatversion"}),
    "color": "*",
    "column": frozenset({"ui", "branch", "clean", "status", "tag"}),
    "status": frozenset({
        "short", "branch", "relativepaths", "showstash", "aheadbehind", "renames", "renamelimit",
        "displaycommentprefix", "submodulesummary", "showuntrackedfiles",
    }),
    "diff": frozenset({
        "renames", "renamelimit", "algorithm", "mnemonicprefix", "noprefix", "indentheuristic", "colormoved",
        "colormovedws", "context", "interhunkcontext", "statgraphwidth", "statnamewidth", "suppressblankempty",
        "ignoresubmodules", "relative", "dirstat", "autorefreshindex", "submodule",
    }),
    "advice": "*",
    "i18n": frozenset({"commitencoding", "logoutputencoding"}),
    "blame": frozenset({"blankboundary", "showroot", "showemail", "date", "coloring", "markunblamablelines",
                        "markignoredlines"}),
    "maintenance": frozenset({"auto", "strategy"}),
    "commit": frozenset({"gpgsign", "verbose", "cleanup", "status"}),
    "tag": frozenset({"gpgsign", "sort", "forcesignannotated"}),
    "gpg": frozenset({"format"}),  # the program comes from the person's global config
    "log": frozenset({"date", "decorate", "abbrevcommit", "follow", "showroot", "mailmap"}),
    "rerere": frozenset({"enabled", "autoupdate"}),
    "index": frozenset({"version", "threads", "skiphash", "sparse"}),
    "feature": frozenset({"manyfiles", "experimental"}),
    "checkout": frozenset({"defaultremote", "workers", "thresholdforparallelism", "guess"}),
    "worktree": frozenset({"guessremote", "userelativepaths"}),
    "transfer": frozenset({"fsckobjects"}),
}  # fmt: skip
#: ``section.<sub>.name``: the names allowed under any subsection of ``section``
ALLOWED_SUB: dict[str, frozenset[str]] = {
    "remote": frozenset({
        "url", "pushurl", "fetch", "push", "tagopt", "prune", "prunetags", "mirror", "skipdefaultupdate",
        "skipfetchall", "promisor", "partialclonefilter", "gh-resolved", "followremotehead",
    }),
    "branch": frozenset({"remote", "merge", "rebase", "pushremote", "description"}),
    "submodule": frozenset({"url", "path", "branch", "active", "ignore", "shallow", "fetchrecursesubmodules"}),
    "lfs": frozenset({"access", "locksverify"}),  # lfs.<url>.access, written by git-lfs itself
    "color": frozenset({"*"}),  # color.<cmd>.<slot>: colours
    "maintenance": frozenset({"enabled", "schedule", "auto"}),  # maintenance.<task>.*
    "gc": frozenset({"reflogexpire", "reflogexpireunreachable"}),  # gc.<pattern>.*
}  # fmt: skip
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


def _allowed(section: str, sub: str | None, name: str, value: str) -> bool:
    if sub is None:
        names = ALLOWED.get(section)
        if names is None or (names != "*" and name not in names):
            return False
        return not ((section, name) == ("core", "fsmonitor") and value.strip().lower() not in _BOOL)
    names_sub = ALLOWED_SUB.get(section)
    return names_sub is not None and ("*" in names_sub or name in names_sub)


def exec_risk(entries: Iterable[tuple[str, str, str]]) -> str | None:
    """The first local or worktree configuration key (``entries`` are ``(scope, key, value)``) that is **not on the
    allow-list** (:data:`ALLOWED`, :data:`ALLOWED_SUB`; a non-boolean ``core.fsmonitor`` is refused too), or ``None``.
    Other scopes are ignored. The rule is an allow-list because the class "a config key that runs a program" is
    open-ended: every git release can add one (:data:`KNOWN_EXEC`)."""
    for scope, key, value in entries:
        if scope not in ("local", "worktree"):
            continue
        section, sub, name = _split(key)
        if not _allowed(section, sub, name, value):
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


def _run(repo: str, args: list[str], own: Mapping[str, str], timeout: float, what: str) -> bytes:
    """One read-only git command of the gate (fixed argv, neutral environment, own process group, killed on timeout).
    Raises :class:`Unsafe` when it cannot run or fails."""
    try:
        proc = subprocess.Popen(  # noqa: S603 - fixed argv
            ["git", *NEUTRAL, "--no-optional-locks", "-C", repo, *args],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, env=gate_env(own), cwd="/",
            start_new_session=True, close_fds=True,
        )  # fmt: skip
    except OSError:
        raise Unsafe(f"git cannot be started to check {what}") from None
    try:
        out, _ = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        with contextlib.suppress(OSError):
            os.killpg(proc.pid, signal.SIGKILL)  # the group this call created, nothing else
        with contextlib.suppress(Exception):
            proc.communicate(timeout=5)
        raise Unsafe(f"checking {what} timed out") from None
    if proc.returncode != 0:
        raise Unsafe(f"{what} cannot be checked")
    return out


def _check_path(path: str, what: str, *, must_exist: bool) -> None:
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        if must_exist:
            raise Unsafe(f"the repository's {what} is missing") from None
        return
    except OSError:
        raise Unsafe(f"the repository's {what} cannot be checked") from None
    if stat.S_ISLNK(st.st_mode):
        raise Unsafe(f"the repository's {what} is a symlink")
    if st.st_uid != os.geteuid() or stat.S_IMODE(st.st_mode) & 0o022:
        raise Unsafe(f"the repository's {what} is not owned by this user or is writable by others")


def config_files(repo: str, own: Mapping[str, str], *, timeout: float = GATE_SECONDS) -> None:
    """Opus #439 D3: the files the listing reads (``<common dir>/config``, ``config.worktree`` of the common and the
    worktree git dir) and the git dirs holding them must be owned by this process's effective UID and writable by no
    group or other (a symlinked one is refused too). git itself checks only the directory's owner; a config file
    another account can write (a shared volume, mode 0666) would let that account change it between the gate and
    git. Raises :class:`Unsafe`."""
    raw = _run(repo, ["rev-parse", "--path-format=absolute", "--git-common-dir", "--git-dir"], own, timeout,
               "the repository's git dir")  # fmt: skip
    lines = raw.decode("utf-8", "surrogateescape").splitlines()
    if len(lines) != 2 or not all(ln.startswith("/") for ln in lines):
        raise Unsafe("the repository's git dir cannot be located")
    common, gitdir = lines
    _check_path(common, "git dir", must_exist=True)
    _check_path(os.path.join(common, "config"), "config file", must_exist=False)
    _check_path(os.path.join(common, "config.worktree"), "config.worktree", must_exist=False)
    if gitdir != common:
        _check_path(gitdir, "worktree git dir", must_exist=True)
        _check_path(os.path.join(gitdir, "config.worktree"), "config.worktree", must_exist=False)


def local_config(repo: str, own: Mapping[str, str], *, timeout: float = GATE_SECONDS) -> list[tuple[str, str, str]]:
    """The local and worktree configuration of the repository at ``repo`` (absolute), listed by git without
    following includes (an ``include.*`` key is listed, and refused, as itself). Raises :class:`Unsafe` when it
    cannot be listed."""
    if not repo.startswith("/") or "\0" in repo:
        raise Unsafe("the repository path is not absolute")
    out = _run(repo, ["config", "--no-includes", "--list", "--show-scope", "-z"], own, timeout,
               "the repository's config")  # fmt: skip
    return [e for e in _parse(out) if e[0] in ("local", "worktree")]


def gate(repo: str, own: Mapping[str, str], *, timeout: float = GATE_SECONDS) -> None:
    """Refuse (:class:`Unsafe`) to let git near ``repo`` when its own configuration sets a key outside the allow-list
    (:func:`exec_risk`, :data:`INVARIANT`) or its config files could be changed by another account
    (:func:`config_files`)."""
    if not repo.startswith("/") or "\0" in repo:
        raise Unsafe("the repository path is not absolute")
    config_files(repo, own, timeout=timeout)
    key = exec_risk(local_config(repo, own, timeout=timeout))
    if key is not None:
        raise Unsafe(f"the repository's own config sets {key}, which is not on orch's allow-list of repository config "
                     "(a key that could make git run a program or read config elsewhere); orch does not run git there "
                     "(remove it, or configure it globally)")  # fmt: skip


def submodules(repo: str, own: Mapping[str, str], *, timeout: float = GATE_SECONDS) -> None:
    """Codex #439 (High, Medium): refuse (:class:`Unsafe`) a working copy with a **populated submodule** (a gitlink in
    the index whose directory is not empty). Its own config is a separate repository's, which the gate does not list,
    and ``git status`` would run its filters; ``--ignore-submodules`` instead hides its changes, so a clean verdict
    would be false. orch does not inspect submodules: such a tree's cleanliness is unknown (as the host's reader,
    which refuses a checked-out submodule). Run :func:`gate` first: this lists the index with git."""
    raw = _run(repo, ["ls-files", "--stage", "-z"], own, timeout, "the repository's index")
    for entry in raw.split(b"\0"):
        if not entry.startswith(b"160000 "):
            continue
        rel = entry.partition(b"\t")[2].decode("utf-8", "surrogateescape")
        path = os.path.join(repo, rel)
        try:
            st = os.lstat(path)
        except FileNotFoundError:
            continue
        except OSError:
            raise Unsafe("a submodule of the repository cannot be checked") from None
        if not stat.S_ISDIR(st.st_mode):
            continue
        try:
            populated = bool(os.listdir(path))
        except OSError:
            populated = True
        if populated:
            raise Unsafe("the repository has a checked-out submodule; orch does not inspect submodules, so its state "
                         "is unknown (check it yourself)")  # fmt: skip
