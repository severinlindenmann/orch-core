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
* **Submodules** (Codex #439): no remaining operation opens a submodule (``staged_raw`` lists gitlinks by id). Every
  cleanliness verdict is orch's reader now, which refuses a populated submodule itself (unknown, never clean; #565).
* **Attributes** (``.gitattributes`` in any tree, ``$GIT_DIR/info/attributes``) only *select* a driver by name; the
  program always comes from configuration. Once the gate passed, the repository's own configuration defines no driver
  program except a tolerated ``diff`` driver, which no orch git command runs (:func:`gate`), so an attribute can
  select only (a) a built-in behaviour (``text``, ``eol``, ``ident``, ``working-tree-encoding``: no program), (b) that
  neutralised ``diff`` driver, or (c) a driver the person configured globally (e.g. ``filter=lfs`` from ``git lfs
  install``), which runs as the person exactly as when they run git themselves. No attribute file is parsed.
* **Kept, defence in depth:** :data:`NEUTRAL` (``-c`` on every command: hooks to ``/dev/null``, fsmonitor off, no
  network protocol, no submodule recursion) and a from-scratch environment (the callers').

**Isolation: the second half of the invariant (#541, #546).** *No git invocation by orch reads configuration the
person or a repo controls, except through an explicit allow-list.* The repo's half is :func:`gate` (above). The
person's half is ONE mechanism here, used by every remaining read-only caller (the gate itself and
``orch.ops.health``'s staged check) and, for its config part, by the records tool:

* :func:`isolated_env`: the whole environment, built from scratch (``HOME``, ``PATH``, ``TMPDIR``, ``USER``,
  ``LOGNAME``, ``XDG_CONFIG_HOME``; never a ``GIT_*`` variable, token or ``ORCH_*`` of the caller), with
  ``GIT_CONFIG_NOSYSTEM=1``, ``GIT_CONFIG_GLOBAL=/dev/null``, no system attributes, no replace objects, no prompt, no
  network protocol, no lazy fetch, ``GIT_PAGER=cat`` and ``GIT_EDITOR=true``;
* :func:`config_env`: configuration passed explicitly as command-scope entries (``GIT_CONFIG_COUNT``), never by
  reading the person's files; no operation of :data:`OPS` keeps any of the person's configuration; the records tool
  keeps its own lists in :func:`orch.records.git.person_config`;
* :func:`command`: **a closed set of operations** (:data:`OPS`, plus Landing's :data:`LAND_OPS`, which run only in the
  host-owned bare repository of :mod:`orch.land.git`; Codex P1 on #561), never free argv: since #565 only
  ``check --staged``'s ``staged_raw`` and ``staged_blob``. It runs :func:`gate` before every operation and builds the
  argv: :data:`HARDEN` (:data:`NEUTRAL` plus :data:`PINNED`), ``--no-pager``, ``--no-optional-locks``, ``-C <repo>``,
  and ``--no-ext-diff --no-textconv`` on ``diff``.

**The gate-then-spawn race (#565) and the class it closes.** :func:`gate` lists the config and git reads it again
when it starts; a concurrent writer of ``.git/config`` (or a worktree's config, or a swapped ``.git`` file) can add,
for example, ``filter.x.clean=<program>`` in between, and ``status`` ran it (reproduced). Filters cannot be switched
off by command-scope config. So **no direct-mode read runs git any more**: repository discovery, ``HEAD``, refs, the
identity, cleanliness, ``orch worktree``'s checks and the dashboard dev host's diff all go through orch's own reader
in-process (:data:`orch.gitread.local.READER`), as host mode's reader children always did. The git processes left
each keep the gate and carry a written RACE NOTE at the call site saying why the window is acceptable there.

**Every git invocation site** and how it meets the invariant:

======================================================  ===========================================================
Site                                                    Isolation
======================================================  ===========================================================
``gitsafe._run`` (the gate's own reads)                 its fixed reads, :func:`isolated_env`, no person keys;
                                                        a config listing and ``rev-parse`` execute nothing
``gitsafe.git_version`` (``git --version``)             :func:`isolated_env`; once per ``PATH``, refuses git < 2.32
``ops/health._git`` (``check --staged``: ``staged_raw``,  :func:`gate` once per check for its message, then
``staged_blob``)                                        :func:`command` (gate again), through ``caps.exec`` (#546).
                                                        RACE NOTE (#565, at :data:`OPS`): no work-tree contents
                                                        read, so no filter; runs inside the person's ``git commit``
``runner/wtgit.run_git`` (``worktree add/remove/        **The one exception** to isolation: :func:`gate` on the repo
prune``)                                                (and the worktree) plus :data:`NEUTRAL`, but the person's
                                                        system and global configuration stays on **by design** (a
                                                        checkout runs their filters, ``git lfs install``'s smudge).
                                                        RACE NOTE (#565, at the spawn): ``remove`` always ``--force``
                                                        after orch's own clean check (git's ``status`` never runs);
                                                        ``prune`` checks out nothing; ``add`` is ``--no-checkout`` and
                                                        orch writes the tree itself (#584, ``gitread.checkout``):
                                                        no process at all; a ``filter=`` attribute refuses
``records/git.Git.run``, ``records/tool`` fast-import   :func:`config_env` with ``person_config``'s lists, the
                                                        records ``HARDEN`` (incl. :data:`PINNED`), ``check_local``
                                                        on top of :func:`exec_risk` (#541); out of #565's scope
                                                        (orch's own records repository, see its note)
``records`` ``git init --template=`` (repo and          system and global config off, no frozen config (#541)
transport)
``records/git.open_repo`` probe                         reads configuration only (executes nothing)
``store/observe``, ``ops/caps.Git`` (direct mode),      **no git process** (#565): :data:`orch.gitread.local.READER`
``hostclient``, ``worktree_cli`` reads, D58 direct,     in-process, config read as data (three keys)
dashboard dev host ``devhost/reads.diff``
``gitread/*`` (host mode)                               no git process (a pure-Python reader in a sandbox child)
======================================================  ===========================================================

``orch.gitbrowse`` (the dashboard's File Explorer, #491) is a second CLOSED set of read-only operations (``log``,
``show``, ``diff``, ``cat-file``, ``grep``) built on this module's pieces (:func:`gate`, :data:`HARDEN`,
:func:`isolated_env`, the diff pins, :func:`_check`): same gate, same isolation, its own fixed templates and operand
slots. It adds no row above because it adds no way to run git outside :func:`gate`.

Two keys are on the allow-list only because a command-scope ``-c`` in :data:`NEUTRAL` overrides them on every command
and command scope always wins: ``core.hooksPath`` (husky and similar tools set it in the local config) and a boolean
``core.fsmonitor`` (``true``/``false``: git's built-in daemon, no configured program). A non-boolean
``core.fsmonitor`` (a program path) is refused.

Used by :mod:`orch.runner.wtgit` (``run_git``: the runner's ``git`` command and the direct-mode ``orch worktree``
tool) and by ``orch check --staged`` (:mod:`orch.ops.health`). The records tool (:mod:`orch.records.git`) applies
:func:`exec_risk` (the same allow-list) first in its own, stricter ``check_local``.
"""

from __future__ import annotations

import contextlib
import contextvars
import hashlib
import os
import re
import signal
import stat
import subprocess
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass

from orch.gitread.refs import branch_ok as _branch_ok  # #561's validator, moved to gitread by #565

__all__ = [
    "ALLOWED", "ALLOWED_SUB", "HARDEN", "INVARIANT", "ISOLATION", "KNOWN_EXEC", "LAND_OPS", "NEUTRAL", "OPS", "PINNED",
    "NetSpec", "Op", "Unsafe", "command", "pinned", "config_env", "config_files", "exec_risk", "gate", "isolated_env",
    "land_check", "land_init", "local_config", "target_tracking_ref",
]  # fmt: skip

INVARIANT = (
    "orch never runs git against a repository whose local/worktree config or attributes could make git execute a "
    "program or read config from elsewhere"
)

ISOLATION = (
    "no git invocation by orch reads configuration the person or a repo controls, except through an explicit allow-list"
)

#: command-scope overrides on every git command (defence in depth; command scope wins over every config file)
NEUTRAL = (
    "-c", "core.hooksPath=/dev/null",
    "-c", "core.fsmonitor=false",
    "-c", "protocol.allow=never",
    "-c", "submodule.recurse=false",
)  # fmt: skip
#: the keys #541 pins on top: not even git's default ``$XDG_CONFIG_HOME/git/attributes`` is read, and a repo's own
#: ``i18n.commitEncoding`` (on the allow-list: it runs nothing) never changes a commit orch writes
PINNED = (
    "-c", "core.attributesFile=/dev/null",
    "-c", "i18n.commitEncoding=UTF-8",
)  # fmt: skip
#: every isolated command (:func:`command`): also no commit-graph (a forged one could change a commit's tree or
#: parents, so the diff a person reviews; the records tool pins it too), and the stat checks at git's defaults (an
#: allowed ``core.trustctime=false`` or ``core.checkStat=minimal`` could hide a same-size change from ``status``)
#: (final Opus delta on #561, MC and L1)
HARDEN = (
    *NEUTRAL, *PINNED, "-c", "core.commitGraph=false", "-c", "core.trustctime=true", "-c", "core.checkStat=default",
    "-c", "advice.graftFileDeprecated=false",  # GIT_GRAFT_FILE=/dev/null makes git print a hint otherwise (R7 I3)
)  # fmt: skip
#: subcommands that take ``--no-ext-diff --no-textconv`` (no diff program, no textconv program)
_NO_PROGRAMS = frozenset({"diff", "diff-tree", "show", "log"})
#: the only variables of the caller an isolated command sees
_KEEP = ("HOME", "PATH", "TMPDIR", "USER", "LOGNAME", "XDG_CONFIG_HOME")

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
        "backend", "rebasemerges", "reschedulefailedexec",  # never rebase.instructionFormat: it can inject exec
    }),
    "submodule": frozenset({"recurse", "active", "fetchjobs", "propagatebranches"}),
    "lfs": frozenset({"repositoryformatversion"}),
    "color": "*",
    "column": frozenset({"ui", "branch", "clean", "status", "tag"}),
    # ``diff``: the named keys stay on the shared allow-list (``exec_risk``, which the records tool applies too, as
    # before #546). The gate applies its own closed list to ``diff.*`` instead (:data:`DIFF_TOLERATED`, driver
    # programs included, each neutralised by every diff operation; any other ``diff.*`` key is refused, see
    # :func:`gate`).
    # ``status.submoduleSummary`` is gone (a summary opens submodules). What else stays is overridden by every
    # operation's fixed options (``status.renames``, ``status.showUntrackedFiles``, ``submodule.<n>.ignore``,
    # ``color.*``: ``--no-renames``, ``--untracked-files``, ``--ignore-submodules``, ``--no-color``), or describes the
    # work tree itself (``core.autocrlf``, ``core.filemode``, ``core.ignoreCase``, as git init writes them).
    "diff": frozenset({
        "renames", "renamelimit", "algorithm", "mnemonicprefix", "noprefix", "indentheuristic", "colormoved",
        "colormovedws", "context", "interhunkcontext", "statgraphwidth", "statnamewidth", "suppressblankempty",
        "ignoresubmodules", "relative", "dirstat", "autorefreshindex", "submodule",
    }),
    "status": frozenset({
        "short", "branch", "relativepaths", "showstash", "aheadbehind", "renames", "renamelimit",
        "displaycommentprefix", "showuntrackedfiles",
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
    """``(section, subsection, name)``: ``None`` for no subsection, and an EMPTY subsection (``[diff ""]``, listed as
    ``diff..name``) kept distinct as ``""``, never read as the top-level key (Opus R7-L1 on #561)."""
    section, _, rest = key.partition(".")
    if "." not in rest:
        return section.lower(), None, rest.lower()
    sub, _, name = rest.rpartition(".")
    return section.lower(), sub, name.lower()


def _allowed(section: str, sub: str | None, name: str, value: str) -> bool:
    if sub == "":  # an empty subsection is no key orch knows
        return False
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


#: The pinned git of a landing run (:func:`pinned`): the executable every git process of this context starts (the
#: gate's version and config reads included, not only the operation) and its ``GIT_EXEC_PATH``. ``None``: ``git`` from
#: ``PATH``, as every other caller of this module.
_PIN: contextvars.ContextVar[tuple[str, str | None] | None] = contextvars.ContextVar("orch_git_pin", default=None)


@contextlib.contextmanager
def pinned(git: str, exec_path: str | None = None):  # noqa: ANN201
    """Run every git process started in this block from ``git`` (absolute, plain path) with ``GIT_EXEC_PATH`` set to
    ``exec_path``: git's own helpers (``index-pack``, ``upload-pack``...) come from the same root-owned place."""
    for v in (git, exec_path):
        if v is not None and (_SAFE_PATH.fullmatch(v) is None or ".." in v.split("/")):
            raise Unsafe("invalid git executable")
    token = _PIN.set((git, exec_path))
    try:
        yield
    finally:
        _PIN.reset(token)


def _git_exe() -> str:
    pin = _PIN.get()
    return pin[0] if pin else "git"


def isolated_env(own: Mapping[str, str], person: Sequence[tuple[str, str]] = ()) -> dict[str, str]:
    """THE environment of every isolated git command (:data:`ISOLATION`, #541, #546): built from scratch out of
    ``own`` (:data:`_KEEP` only: never a ``GIT_*`` variable, a token or an ``ORCH_*`` variable of the caller), system
    and global configuration off, and of the person's configuration exactly ``person`` (already filtered by the
    caller's own allow-list; no operation of :data:`OPS` has any since #565), passed by :func:`config_env`."""
    env = {k: own[k] for k in _KEEP if own.get(k) and "\0" not in own[k]}
    env.setdefault("PATH", "/usr/bin:/bin:/usr/local/bin")
    env.update(
        LANG="C", LC_ALL="C", GIT_TERMINAL_PROMPT="0", GIT_OPTIONAL_LOCKS="0", GIT_ALLOW_PROTOCOL="",
        GIT_NO_LAZY_FETCH="1", GIT_ATTR_NOSYSTEM="1", GIT_NO_REPLACE_OBJECTS="1", GIT_PAGER="cat", GIT_EDITOR="true",
        GIT_GRAFT_FILE="/dev/null",  # info/grafts could change a commit's parents, so a diff's base (MC)
    )  # fmt: skip
    env.update(config_env(person))
    pin = _PIN.get()
    if pin is not None and pin[1] is not None:
        env["GIT_EXEC_PATH"] = pin[1]
    return env


def gate_env(own: Mapping[str, str]) -> dict[str, str]:
    """The gate's own environment: :func:`isolated_env` with no person keys."""
    return isolated_env(own)


def config_env(entries: Sequence[tuple[str, str]]) -> dict[str, str]:
    """System and global configuration off, and ``entries`` (the caller's allow-listed subset of the person's
    configuration) passed as command-scope configuration: the one way the person's configuration reaches orch's
    git (the records tool uses it too)."""
    env = {"GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_COUNT": str(len(entries))}
    for i, (k, v) in enumerate(entries):
        env[f"GIT_CONFIG_KEY_{i}"] = k
        env[f"GIT_CONFIG_VALUE_{i}"] = v
    return env


def _argv(repo: str, args: Sequence[str], op_pins: Sequence[str] = ()) -> list[str]:
    """The argv of one isolated git command against ``repo`` (absolute): :data:`HARDEN`, no pager, no optional locks,
    and ``--no-ext-diff --no-textconv`` right after ``diff``, ``show`` or ``log``. Private: ``args`` are only ever the
    gate's own fixed reads or an operation of :data:`OPS` (:func:`command`)."""
    rest = list(args)
    pins: tuple[str, ...] = ()
    if rest and rest[0] in _NO_PROGRAMS:
        rest[1:1] = ["--no-ext-diff", "--no-textconv"]
        pins = _DIFF_PINS
    return [_git_exe(), *HARDEN, *pins, *op_pins, "--no-pager", "--no-optional-locks", "-C", repo, *rest]


@dataclass(frozen=True)
class Op:
    """One read-only git operation: fixed arguments, some holding a slot for a validated operand (``<path>``: a path
    in the index, relative to ``-C <repo>``)."""

    args: tuple[str, ...]
    #: extra command-scope ``-c`` pairs of this operation only (after :data:`HARDEN`)
    pins: tuple[str, ...] = ()
    #: the caller's variables this operation may be given on top of :func:`isolated_env` (never anything else)
    env: frozenset[str] = frozenset()
    #: the operation talks to a remote (Landing's ``land_push``/``land_fetch``, the ONLY two): it needs a
    #: :class:`NetSpec` and is the only kind that is given one
    net: bool = False

    @property
    def operands(self) -> tuple[str, ...]:
        """The kinds of the operands, in order."""
        return tuple(m for a in self.args for m in _SLOT.findall(a))


_SLOT = re.compile(r"<(rev|path|context|oid|lref|ref|tref|url)>")

#: every diff between commits: never relative to a subdirectory, never a submodule opened for its log or its diff
#: (``--submodule=short`` beats an allowed-looking ``diff.submodule``), no colour, pointer moves visible
_DIFF_FIXED = (
    "--no-relative", "--submodule=short", "--no-color", "--ignore-submodules=dirty", "-O/dev/null",
    "--diff-algorithm=default",  # beats even a driver's own algorithm, which -c diff.algorithm does not (R7-L1)
)  # fmt: skip
#: Every diff operation also gets these command-scope values (command scope beats the repo's own config), so a
#: ``diff.*`` key cannot run a program or change the output orch parses (the gate tolerates only
#: :data:`DIFF_TOLERATED`; pinning more costs nothing). Each named key of
#: ``git help config``'s ``diff`` section, and why:
#:
#: * run a program: ``diff.external``, ``diff.<driver>.command`` (``--no-ext-diff``), ``diff.<driver>.textconv``
#:   (``--no-textconv``), both added by :func:`_argv` to every ``diff``; ``diff.<driver>.cachetextconv`` is then
#:   unused; ``diff.trustExitCode``, ``diff.tool``, ``diff.guitool``: difftool only, never run.
#: * what is opened or listed: ``diff.submodule`` (pinned ``short`` and ``--submodule=short``), ``diff.relative``
#:   (pinned ``false``; ``--no-relative``, or ``--relative`` for ``staged_raw``), ``diff.ignoreSubmodules``
#:   (pinned ``none``; every diff passes ``--ignore-submodules``), ``diff.orderFile`` (``-O/dev/null`` on every diff),
#:   ``diff.renames`` and ``diff.renameLimit`` (pinned ``true`` and ``1000``; ``--no-renames`` or ``-M50%``).
#: * the text: ``diff.noprefix``, ``diff.mnemonicPrefix``, ``diff.srcPrefix``, ``diff.dstPrefix`` (pinned off and
#:   ``a/``/``b/``), ``diff.context`` (``-U<n>``), ``diff.interHunkContext`` (``0``), ``diff.algorithm``
#:   (``default``), ``diff.indentHeuristic`` (``true``), ``diff.suppressBlankEmpty`` (``false``),
#:   ``color.diff``/``color.ui``, ``diff.colorMoved``, ``diff.colorMovedWS``, ``diff.wsErrorHighlight`` (no colour:
#:   ``--no-color``, and pinned off).
#: * harmless here: ``diff.autoRefreshIndex`` (only a diff against the work tree, which no operation runs),
#:   ``diff.dirstat``, ``diff.statNameWidth``, ``diff.statGraphWidth`` (no ``--stat``), ``diff.wordRegex`` and
#:   ``diff.<driver>.wordRegex`` (no ``--word-diff``).
#: * not pinnable, no program: ``diff.<driver>.xfuncname``/``funcname`` (the text after ``@@`` in a hunk header) and
#:   ``diff.<driver>.binary`` (a file a repo's attribute marks binary is shown as binary). They change only what the
#:   repo's own attributes ask for and run nothing.
_DIFF_PINS = tuple(
    arg
    for kv in (
        "diff.noprefix=false", "diff.mnemonicPrefix=false", "diff.srcPrefix=a/", "diff.dstPrefix=b/",
        "diff.relative=false", "diff.submodule=short", "diff.ignoreSubmodules=none", "diff.renames=true",
        "diff.renameLimit=1000", "diff.algorithm=default", "diff.indentHeuristic=true", "diff.context=3",
        "diff.interHunkContext=0", "diff.suppressBlankEmpty=false", "diff.colorMoved=no", "diff.colorMovedWS=no",
        "diff.wsErrorHighlight=none", "color.diff=false", "color.ui=false",
    )
    for arg in ("-c", kv)
)  # fmt: skip

#: The CLOSED set of git operations orch may run to READ a repository (#546, Codex P1 on #561), and since #565 only
#: ``check --staged``'s two (``orch.ops.health``): every other direct-mode read moved to orch's own reader in-process
#: (:data:`orch.gitread.local.READER`), which starts no process. There is no free argv: no caller can add a global
#: option (``-c``, ``-C``, ``--git-dir``, ``--work-tree``, ``--exec-path``, ``--namespace``, ``-p``,
#: ``--config-env``), an option of its own or a subcommand, and no operation can reach the network. A new read must be
#: a reader request, not an operation here: an operation keeps the gate-then-spawn window of #565.
#:
#: RACE NOTE (#565): :func:`command` gates the config and the caller spawns git afterwards; a concurrent writer of the
#: config could change it in between. Accepted for these two because neither reads a work-tree file's contents, so no
#: clean or smudge filter, textconv or diff driver applies: ``staged_raw`` compares the index with ``HEAD`` by object
#: id (``--raw``, no patch; ``--ignore-submodules=dirty`` opens no submodule), ``staged_blob`` prints a stored blob as
#: stored (``cat-file blob``, no ``--filters``, no ``--textconv``). And they run inside the person's own ``git
#: commit`` (the pre-commit hook), which has already read and run that same repository config as the person.
OPS: dict[str, Op] = {
    "staged_raw": Op(("diff", "--cached", "--raw", "-z", "--no-renames", "--no-color", "--relative",
                      "--ignore-submodules=dirty", "--submodule=short", "-O/dev/null", "--diff-algorithm=default")),
    # paths of staged_raw are relative to -C <repo> (--relative): ":0:./<path>" resolves them there, not at the top
    "staged_blob": Op(("cat-file", "blob", ":0:./<path>")),
}  # fmt: skip

#: git's own object checks that must be errors for a landing, set explicitly (``--strict`` and the version's defaults
#: leave some at info/warn): path names git cannot or must not check out (``.git``, empty, ``.``, ``..``, ``/``,
#: HFS-ignorable and NTFS spellings are inside ``hasDotgit``/``gitmodulesName``), symlinked ``.gitmodules``,
#: ``.gitattributes``, ``.gitignore``, ``.mailmap``, a malformed or unsafe ``.gitmodules``, unsorted or duplicate
#: entries.
FSCK_ERRORS = (
    "hasDot",
    "hasDotdot",
    "hasDotgit",
    "emptyName",
    "fullPathname",
    "nullSha1",
    "zeroPaddedFilemode",
    "duplicateEntries",
    "treeNotSorted",
    "gitmodulesSymlink",
    "gitattributesSymlink",
    "gitignoreSymlink",
    "mailmapSymlink",
    "gitmodulesMissing",
    "gitmodulesBlob",
    "gitmodulesLarge",
    "gitmodulesName",
    "gitmodulesParse",
    "gitmodulesPath",
    "gitmodulesUrl",
    "gitmodulesUpdate",
    "badFilemode",
    "badName",
    "gitattributesLarge",
    "gitattributesLineLength",
    "gitattributesMissing",
    "gitattributesBlob",
    "largePathname",
    "nulInCommit",
    "badTree",
)

#: Landing's operations (#596), a second CLOSED set beside :data:`OPS` (kept apart so :data:`OPS`, which the isolation
#: tests walk against ordinary work-tree repositories, stays what it was). ``land_*`` runs ONLY in the host-owned bare
#: repository of :mod:`orch.land.git` (:func:`land_check` on top of the gate, in :func:`command`); agent repositories
#: are read by gitread, never here. Every revision is a full object id (``<oid>``), the only ref is ``refs/land/<id>``;
#: no operation touches the network (that is task 4's ``land_push``/``land_fetch``) or a work tree.
LAND_OPS: dict[str, Op] = {
    "land_has": Op(("cat-file", "-e", "<oid>")),
    "land_commit": Op(("cat-file", "commit", "<oid>")),
    "land_merge_base": Op(("merge-base", "<oid>", "<oid>")),
    "land_is_ancestor": Op(("merge-base", "--is-ancestor", "<oid>", "<oid>")),
    "land_count": Op(("rev-list", "--count", "<oid>..<oid>")),
    # a merge computed from objects only (no index, no work tree); exit 1 = conflicts (paths listed)
    # Every merge knob the allow-list tolerates in a repository's config is pinned (Codex #4 on #761), so the tree is a
    # function of the two commits only (I4), whatever the host repository's own config says.
    "land_merge_tree": Op(
        ("merge-tree", "--write-tree", "-z", "--name-only", "--no-messages", "<oid>", "<oid>"),
        pins=(
            "-c", "merge.renormalize=false", "-c", "merge.renames=true", "-c", "merge.renameLimit=1000",
            "-c", "merge.directoryRenames=conflict", "-c", "merge.conflictStyle=merge", "-c", "diff.renames=true",
            "-c", "diff.renameLimit=1000", "-c", "diff.algorithm=default", "-c", "core.autocrlf=false",
            "-c", "core.eol=lf", "-c", "core.safecrlf=false",
        ),
    ),  # fmt: skip
    # message on stdin; identity and dates only from the caller's GIT_AUTHOR_* / GIT_COMMITTER_* (I4: deterministic)
    "land_commit_tree": Op(
        ("commit-tree", "--no-gpg-sign", "-p", "<oid>", "-p", "<oid>", "-F", "-", "<oid>"),
        pins=("-c", "commit.gpgsign=false"),
        env=frozenset({"GIT_AUTHOR_NAME", "GIT_AUTHOR_EMAIL", "GIT_AUTHOR_DATE",
                       "GIT_COMMITTER_NAME", "GIT_COMMITTER_EMAIL", "GIT_COMMITTER_DATE"}),
    ),  # fmt: skip
    "land_diff_raw": Op(("diff-tree", "-r", "--raw", "-z", "--no-renames", "--ignore-submodules=none", "--no-color",
                         "<oid>", "<oid>")),  # fmt: skip
    # git's own strict check of the INCOMING objects only (like a receive: ``index-pack --strict`` on the pack the
    # importer built, run with the quarantine as object directory and the host's objects as alternate): the check that
    # decides which tree entries are acceptable; the importer's own name check is only an early refusal. Every relevant
    # message id is an error. The pack arrives on stdin; git writes it and its index into the quarantine.
    "land_index_pack": Op(
        ("index-pack", "--stdin", "--strict=" + ",".join(f"{i}=error" for i in FSCK_ERRORS)),
        env=frozenset({"GIT_OBJECT_DIRECTORY", "GIT_ALTERNATE_OBJECT_DIRECTORIES"}),
    ),
    # fold the packs the importer added into one (``-a -d``); ``-k`` keeps objects no ref reaches (imported source
    # commits are not refs): only ever objects git already accepted, nothing is dropped. No bitmap, no size split.
    "land_repack": Op(
        ("repack", "-a", "-d", "-k", "-q", "--no-write-bitmap-index"),
        pins=("-c", "pack.packSizeLimit=0", "-c", "gc.auto=0", "-c", "repack.useDeltaIslands=false",
              "-c", "repack.packKeptObjects=false", "-c", "pack.threads=1"),
    ),  # fmt: skip
    "land_update_ref": Op(("update-ref", "--no-deref", "<lref>", "<oid>")),
    # what the last fetch left under ``refs/land/target/<sha256 of the full ref>`` (read only; exit 1 = none yet)
    "land_ref": Op(("rev-parse", "--verify", "--quiet", "--end-of-options", "<tref>^{commit}")),
    # -- the ONLY two operations with a network (task 4): ssh only, a pinned host key, a deploy key, no agent, no
    # config (see :class:`NetSpec`). The remote and ref come from signed settings and are checked here, never taken
    # from the repository's own configuration (``remote.<n>.url`` is not read: the URL is an operand after ``--``).
    # Fetch brings exactly one branch into ``refs/land/target/...`` (a local ref only this code reads), objects checked
    # by git's own fsck with the same messages as an import.
    "land_fetch": Op(
        ("fetch", "--no-tags", "--no-recurse-submodules", "--no-write-fetch-head", "--no-auto-gc",
         "--no-auto-maintenance", "--no-show-forced-updates", "--no-prune", "--", "<url>", "+<ref>:<tref>"),
        pins=("-c", "protocol.ssh.allow=always", "-c", "transfer.fsckObjects=true", "-c", "fetch.fsckObjects=true",
              "-c", "gc.auto=0", "-c", "maintenance.auto=false", "-c", "fetch.writeCommitGraph=false",
              *(a for i in FSCK_ERRORS for a in ("-c", f"fsck.{i}=error"))),
        net=True,
    ),  # fmt: skip
    # Push the candidate to the target branch. NO ``+`` and no ``--force``: ``--force-with-lease=<ref>:<expected>`` is
    # the exact compare-and-swap on the remote value (Opus F2/#11), and because a lease lifts git's own fast-forward
    # check, :func:`orch.land.remote.push` asserts ancestry (and :meth:`LandRepo.check_push_shape`) first. ``--atomic``
    # so nothing else moves; the source is always a full object id (never an empty source, which would delete).
    "land_push": Op(
        ("push", "--atomic", "--porcelain", "--no-verify", "--no-signed", "--no-recurse-submodules",
         "--force-with-lease=<ref>:<oid>", "--", "<url>", "<oid>:<ref>"),
        pins=("-c", "protocol.ssh.allow=always", "-c", "push.default=nothing", "-c", "push.followTags=false",
              "-c", "push.gpgSign=false", "-c", "push.negotiate=false", "-c", "push.recurseSubmodules=no",
              "-c", "gc.auto=0", "-c", "maintenance.auto=false"),
        net=True,
    ),  # fmt: skip
}  # fmt: skip


#: a revision operand: ``HEAD``, a full commit id, or ``refs/heads/<name>`` with ``<name>`` checked by
#: :func:`_branch_ok` (git's own ref rules, Unicode allowed). No operation of :data:`OPS` takes one since #565;
#: :mod:`orch.gitbrowse` (the explorer's own closed operation table) still does.
_REV = re.compile(r"HEAD|[0-9a-f]{40}|[0-9a-f]{64}|refs/heads/(?P<name>.+)", re.DOTALL)


def _check(kind: str, value: object) -> str:
    if not isinstance(value, str) or not value or "\0" in value or "\n" in value:
        raise Unsafe("invalid git operand")
    if kind == "rev":
        m = _REV.fullmatch(value)
        if m is None or (m.group("name") is not None and not _branch_ok(m.group("name"))):
            raise Unsafe("invalid revision")
    elif kind == "context":
        if not re.fullmatch(r"[0-9]|10", value):
            raise Unsafe("invalid context")
    elif kind == "oid":  # a full object id, never a name: no ref, no ``^``, no range
        if not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", value):
            raise Unsafe("invalid object id")
    elif kind == "ref":  # a target branch: a full ``refs/heads/<name>``, ``<name>`` by git's own ref rules
        m = re.fullmatch(r"refs/heads/(.+)", value, re.DOTALL)
        if m is None or not _branch_ok(m.group(1)):
            raise Unsafe("invalid branch ref")
    elif kind == "tref":  # where a fetched target lands: ``refs/land/target/<sha256 hex of the full ref name>``
        if not re.fullmatch(r"refs/land/target/[0-9a-f]{64}", value):
            raise Unsafe("invalid landing target ref")
    elif kind == "url":
        _check_url(value)
    elif kind == "lref":  # the one namespace the landing code writes in its own repository: ``refs/land/<id>``
        if not re.fullmatch(r"refs/land/(?:[0-9a-f]{40}|[0-9a-f]{64})", value):
            raise Unsafe("invalid landing ref")
    elif value.startswith("/") or len(value) > 4096 or ".." in value.split("/"):  # relative to -C <repo>, below it
        raise Unsafe("invalid path")
    return value


#: ``ssh://<user>@<host>[:<port>]/<absolute path>``: the one remote shape (an scp-like ``host:path``, ``file://``,
#: ``http(s)``, ``ext::`` or a plain path never reaches git). No user-info password, no IPv6 literal, no ``..``, a host
#: that cannot start like an ssh option or a dot.
_URL = re.compile(
    r"ssh://[A-Za-z_][A-Za-z0-9_.-]{0,31}@[A-Za-z0-9][A-Za-z0-9.-]{0,252}(?::[0-9]{1,5})?/[A-Za-z0-9._~+/-]{1,1024}"
)  # fmt: skip


def _check_url(value: str) -> None:
    m = _URL.fullmatch(value)
    if m is None or ".." in value.split("/") or "//" in value[len("ssh://") :]:
        raise Unsafe("invalid remote url")
    port = re.search(r":([0-9]{1,5})/", value.split("@", 1)[1])
    if port is not None and not 0 < int(port.group(1)) < 65536:
        raise Unsafe("invalid remote url")


def target_tracking_ref(ref: str) -> str:
    """Where :data:`LAND_OPS` ``land_fetch`` puts the remote branch ``ref`` (``refs/heads/<name>``): a name that is a
    function of the full ref and never forms a path of its own (a branch name cannot collide, nest or escape)."""
    _check("ref", ref)
    return "refs/land/target/" + hashlib.sha256(ref.encode("utf-8", "surrogatepass")).hexdigest()


_SAFE_PATH = re.compile(r"/[A-Za-z0-9_./@+=,-]{1,4000}")


@dataclass(frozen=True)
class NetSpec:
    """What a network operation (``land_fetch``/``land_push``) needs, built by :mod:`orch.land.key` after its custody
    checks: ``git`` and ``ssh`` (absolute, root-owned executables, never found on a ``PATH``), the deploy ``key`` file
    and the ``known_hosts`` pin of this one remote. Paths are plain (no space, quote or shell character: they are part
    of an ssh command line)."""

    git: str
    ssh: str
    key: str
    known_hosts: str

    def __post_init__(self) -> None:
        for v in (self.git, self.ssh, self.key, self.known_hosts):
            if not isinstance(v, str) or _SAFE_PATH.fullmatch(v) is None or ".." in v.split("/"):
                raise Unsafe("invalid network specification")

    def ssh_command(self) -> str:
        """``GIT_SSH_COMMAND``: built here and nowhere else. ``-F /dev/null`` (no user or system ssh config: no
        ``ProxyCommand``, ``Include``, ``Match exec``), only this key (``IdentitiesOnly``, no agent), only the pinned
        host key (strict; no global file; no rewriting the pin; no IP entries), no prompt, no forwarding, no
        multiplexing, no jump, no local command."""
        opts = (
            ("-i", self.key), ("-F", "/dev/null"),
            ("-o", "IdentitiesOnly=yes"), ("-o", "IdentityAgent=none"),
            ("-o", f"UserKnownHostsFile={self.known_hosts}"),
            ("-o", "GlobalKnownHostsFile=/dev/null"), ("-o", "StrictHostKeyChecking=yes"),
            ("-o", "UpdateHostKeys=no"), ("-o", "CheckHostIP=no"), ("-o", "VerifyHostKeyDNS=no"),
            ("-o", "BatchMode=yes"), ("-o", "PasswordAuthentication=no"), ("-o", "KbdInteractiveAuthentication=no"),
            ("-o", "ForwardAgent=no"), ("-o", "ForwardX11=no"), ("-o", "ClearAllForwardings=yes"),
            ("-o", "ControlMaster=no"), ("-o", "ControlPath=none"), ("-o", "ProxyCommand=none"),
            ("-o", "ProxyJump=none"), ("-o", "PermitLocalCommand=no"), ("-o", "LocalCommand=none"),
            ("-o", "CanonicalizeHostname=no"), ("-o", "ConnectTimeout=20"), ("-o", "ServerAliveInterval=15"),
            ("-o", "ServerAliveCountMax=3"), ("-o", "LogLevel=ERROR"),
        )  # fmt: skip
        return " ".join([self.ssh, *(a for pair in opts for a in pair)])


def _net_env(net: NetSpec) -> dict[str, str]:
    """The environment of a network operation on top of :func:`isolated_env`: ssh only, our own ssh command, and a
    ``PATH`` of the system directories (git finds its helpers next to itself; nothing user-writable)."""
    return {
        "GIT_ALLOW_PROTOCOL": "ssh", "GIT_SSH_COMMAND": net.ssh_command(), "GIT_SSH_VARIANT": "ssh",
        "PATH": "/usr/bin:/bin",
    }  # fmt: skip


def _fill(op: Op, operand: str | tuple[str, ...] | None) -> list[str]:
    """``op``'s arguments with each slot replaced by its validated operand (a path only ever lands in the object name
    ``:0:<path>``: never an option, never a stage or a ``:/`` search)."""
    if operand is not None and not isinstance(operand, (str, tuple)):
        raise Unsafe("invalid git operand")
    values = () if operand is None else (operand,) if isinstance(operand, str) else operand
    if len(values) != len(op.operands):
        raise Unsafe("wrong number of git operands")
    it = iter(values)
    return [_SLOT.sub(lambda m: _check(m.group(1), next(it)), a) for a in op.args]


def command(
    repo: str,
    op: str,
    own: Mapping[str, str],
    *,
    operand: str | tuple[str, ...] | None = None,
    timeout: float = GATE_SECONDS,
    env_extra: Mapping[str, str] | None = None,
    net: NetSpec | None = None,
    git: str | None = None,
    exec_path: str | None = None,
) -> tuple[list[str], dict[str, str]]:
    """The argv and environment of operation ``op`` (see :func:`_command`). A landing operation passes ``git`` (and
    ``exec_path``): the pinned executable runs the gate's own reads and the operation, never whatever ``PATH`` names
    (Codex on #762, Opus M1 on #762)."""
    if git is None and net is None:
        return _command(repo, op, own, operand=operand, timeout=timeout, env_extra=env_extra, net=net)
    if op not in LAND_OPS or (net is not None and git is not None and git != net.git):
        raise Unsafe("invalid git executable")
    with pinned(git if git is not None else net.git, exec_path):  # type: ignore[union-attr]
        return _command(repo, op, own, operand=operand, timeout=timeout, env_extra=env_extra, net=net)


def _command(
    repo: str,
    op: str,
    own: Mapping[str, str],
    *,
    operand: str | tuple[str, ...] | None = None,
    timeout: float = GATE_SECONDS,
    env_extra: Mapping[str, str] | None = None,
    net: NetSpec | None = None,
) -> tuple[list[str], dict[str, str]]:
    """The argv and environment of operation ``op`` of :data:`OPS` against ``repo`` (absolute), for the caller to
    spawn (``cwd="/"``, stdin closed). Runs :func:`gate` first, every time; none of the person's configuration reaches
    git. Raises :class:`Unsafe` (git must not run) for an unknown operation, an invalid operand or a refusal. The
    race between the gate and the spawn is accepted only for the operations of :data:`OPS` (its race note, #565)."""
    spec = OPS.get(op) or LAND_OPS.get(op)
    if spec is None:
        raise Unsafe("unknown git operation")
    if spec.net != (net is not None):  # a network operation always has its spec, no other operation ever does
        raise Unsafe("invalid network specification")
    args = _fill(spec, operand)
    extra = dict(env_extra or {})
    if not set(extra) <= spec.env or "GIT_DIR" in extra or any("\0" in v for v in extra.values()):
        raise Unsafe("invalid git environment")
    local = gate(repo, own, timeout=timeout)
    if op in LAND_OPS:
        land_check(repo, local)
        _land_env_ok(repo, extra)
        extra["GIT_DIR"] = repo  # git never discovers a repository: this one, and no other, no ``.git`` redirection
    entries = _driver_pins(local) if spec.args[0] in ("diff", "diff-tree") else []
    argv = _argv(repo, args, spec.pins)
    env = {**isolated_env(own, entries), **extra}
    if net is not None:
        env.update(_net_env(net))
    return argv, env


def land_check(repo: str, local: Iterable[tuple[str, str, str]]) -> None:
    """The landing-specific half of the gate (Codex #2 on the Landing design): the repository of an operation named
    ``land_*`` is the host-owned **bare** repository (``core.bare=true``, a real directory path, no symlinked top), with
    no alternates, no shallow file and no grafts: nothing that makes git read objects or history from elsewhere. Raises
    :class:`Unsafe`. (Config keys are already the gate's allow-list: no hook, include, filter or merge driver.)"""
    if os.path.realpath(repo) != repo:
        raise Unsafe("the landing repository path is not a real path")
    bare = [v.strip().lower() for _s, k, v in local if k.lower() == "core.bare"]
    if not bare or bare[-1] not in ("true", "yes", "on", "1"):  # git's last value wins
        raise Unsafe("the landing repository is not bare")
    if any(_split(k)[0] in ("remote", "url") for _s, k, _v in local):
        # git resolves a URL operand that spells a configured remote's name through that remote (``remote.<url>.url``),
        # so a repo that names a remote could redirect a network operation (Codex on #762); the host repository has none
        raise Unsafe("the landing repository defines a remote")
    for rel in ("objects", "refs", "HEAD"):
        _check_path(os.path.join(repo, rel), f"landing {rel}", must_exist=True)
    for rel in (
        "objects/info/alternates", "objects/info/http-alternates", "shallow", "info/grafts", "info/attributes",
        ".git", "commondir", "gitdir",
    ):  # fmt: skip
        if os.path.lexists(os.path.join(repo, rel)):
            raise Unsafe(f"the landing repository has {rel}")


def _land_env_ok(repo: str, extra: Mapping[str, str]) -> None:
    """The object-directory variables an operation may carry (the import quarantine, like git's receive quarantine):
    the directory must be ``<repo>/incoming-<hex>`` and the alternate exactly ``<repo>/objects``; nothing else."""
    for key, value in extra.items():
        if key == "GIT_OBJECT_DIRECTORY":
            ok = re.fullmatch(re.escape(repo) + r"/incoming-[0-9a-f]{16}", value) is not None
        elif key == "GIT_ALTERNATE_OBJECT_DIRECTORIES":
            ok = value == f"{repo}/objects"
        else:
            ok = True  # the identity variables of land_commit_tree: free text, never a path git reads
        if not ok:
            raise Unsafe("invalid git environment")


def land_init(path: str, own: Mapping[str, str]) -> tuple[list[str], dict[str, str]]:
    """argv and environment that create the bare landing repository at ``path`` (absolute, clean, not yet a
    repository): no template (so no hook, no sample file), SHA-1, an isolated environment. The caller spawns it."""
    if not path.startswith("/") or "\0" in path or len(path) > 4096 or ".." in path.split("/"):
        raise Unsafe("invalid landing repository path")
    return (
        [_git_exe(), *HARDEN, "init", "--bare", "--quiet", "--template=", "--object-format=sha1",
         "--initial-branch=orch-land", "--", path],
        isolated_env(own),
    )  # fmt: skip


#: git's built-in diff drivers: its userdiff set AS OF GIT 2.54 (userdiff.c, 27 drivers). An attribute can select them
#: with no repo config at all. Their regexes are git's own (no backreferences), but the rule is simpler when it covers
#: every driver git can select (Opus R8 I1): they are pinned like the repo's own. A built-in that a newer git adds
#: stays unpinned until it is listed here; that is harmless because its regex is git's, and since #565 the only diff
#: operation left (``staged_raw``, ``--raw``) prints no hunk at all.
BUILTIN_DRIVERS = (
    "ada", "bash", "bibtex", "cpp", "csharp", "css", "dts", "elixir", "fortran", "fountain", "golang", "html", "ini",
    "java", "kotlin", "markdown", "matlab", "objc", "pascal", "perl", "php", "python", "r", "ruby", "rust", "scheme",
    "tex",
)  # fmt: skip


def _driver_pins(local: Iterable[tuple[str, str, str]]) -> list[tuple[str, str]]:
    """For every diff driver the repository's own config defines (``diff.<driver>.*``, tolerated by the gate) and every
    built-in one (:data:`BUILTIN_DRIVERS`), as
    command-scope configuration passed through ``GIT_CONFIG_COUNT`` (no parsing of the driver's name):

    * ``diff.<driver>.binary=false``: a repo-forced binary driver changes nothing git prints;
    * ``diff.<driver>.xfuncname=^$`` and ``diff.<driver>.funcname=^$``: the repo's regex never runs (a backtracking
      one stalled a patch until the timeout, Opus R7-M1).

    Since #565 the one diff operation is ``staged_raw`` (``--raw``: no patch, no hunk); the pins stay as defence in
    depth, so a diff operation added later cannot forget them."""
    own = {sub for _scope, key, _v in local if (s := _split(key))[0] == "diff" and (sub := s[1])}
    drivers = sorted(own | set(BUILTIN_DRIVERS))
    return [(f"diff.{d}.{name}", value) for d in drivers
            for name, value in (("binary", "false"), ("xfuncname", "^$"), ("funcname", "^$"))]  # fmt: skip


def _run(repo: str, args: list[str], own: Mapping[str, str], timeout: float, what: str) -> bytes:
    """One read-only git command of the gate (fixed argv, neutral environment, own process group, killed on timeout).
    Raises :class:`Unsafe` when it cannot run or fails."""
    try:
        proc = subprocess.Popen(  # noqa: S603 - fixed argv
            _argv(repo, args),
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, env=isolated_env(own), cwd="/",
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


#: the oldest git whose isolation holds: ``GIT_CONFIG_GLOBAL`` came in 2.32 (``GIT_CONFIG_COUNT`` in 2.31); an older
#: git ignores both silently and would read the person's global configuration again (Opus delta on #561, Info 6)
MIN_GIT = (2, 32)
_VERSIONS: dict[str, tuple[int, int]] = {}


def git_version(own: Mapping[str, str], *, timeout: float = GATE_SECONDS) -> tuple[int, int]:
    """The ``(major, minor)`` of the git on ``own``'s ``PATH`` (asked once per ``PATH``); :class:`Unsafe` when it is
    older than :data:`MIN_GIT` or cannot be told."""
    exe = _git_exe()
    key = isolated_env(own)["PATH"] if _PIN.get() is None else f"{exe}|{isolated_env(own)['PATH']}"
    got = _VERSIONS.get(key)
    if got is None:
        try:
            done = subprocess.run(  # noqa: S603 - fixed argv
                [exe, "--version"], stdin=subprocess.DEVNULL, capture_output=True, env=isolated_env(own), cwd="/",
                timeout=timeout, check=False, start_new_session=True,
            )  # fmt: skip
        except (OSError, subprocess.SubprocessError):
            raise Unsafe("git cannot be started to check its version") from None
        m = re.match(rb"git version (\d+)\.(\d+)", done.stdout)
        if done.returncode != 0 or m is None:
            raise Unsafe("git's version cannot be told")
        got = (int(m.group(1)), int(m.group(2)))
        _VERSIONS[key] = got
    if got < MIN_GIT:
        raise Unsafe(
            f"git {got[0]}.{got[1]} is too old: orch needs git 2.32 or newer (older git silently ignores "
            "GIT_CONFIG_GLOBAL, so the person's configuration could not be kept out); update git"
        )
    return got


#: The ``diff.*`` keys of a repository's own config the gate tolerates (orch-52, #561), each with what neutralises it
#: in every diff operation of :data:`OPS`. Any other ``diff.*`` key is refused (:func:`gate`).
DIFF_TOLERATED: dict[str, str] = {
    "algorithm": "pinned diff.algorithm=default (_DIFF_PINS)",
    "indentheuristic": "pinned diff.indentHeuristic=true (_DIFF_PINS)",
    "context": "-U<n> on diff_patch; pinned diff.context=3 (_DIFF_PINS)",
    "interhunkcontext": "pinned diff.interHunkContext=0 (_DIFF_PINS)",
    "renames": "--no-renames, or -M50% where renames are reported; pinned diff.renames=true",
    "submodule": "--submodule=short; pinned diff.submodule=short",
    "relative": "--no-relative (staged_raw: --relative); pinned diff.relative=false",
    "noprefix": "--src-prefix=a/ --dst-prefix=b/ on diff_patch; pinned diff.noprefix=false",
    "mnemonicprefix": "--src-prefix=a/ --dst-prefix=b/ on diff_patch; pinned diff.mnemonicPrefix=false",
    "srcprefix": "--src-prefix=a/ on diff_patch; pinned diff.srcPrefix=a/",
    "dstprefix": "--dst-prefix=b/ on diff_patch; pinned diff.dstPrefix=b/",
    "external": "--no-ext-diff on every diff (_argv)",
}
#: ``diff.<driver>.<name>``: the names tolerated under any driver
DIFF_DRIVER_TOLERATED: dict[str, str] = {
    "textconv": "--no-textconv on every diff (_argv); git-lfs sets diff.lfs.textconv",
    "cachetextconv": "unused: --no-textconv",
    "command": "--no-ext-diff on every diff (_argv)",
    "binary": "pinned diff.<driver>.binary=false for every driver the repo defines (_driver_pins); --text on the patch",
    "xfuncname": "pinned ^$ for every driver the repo defines (_driver_pins): the repo's regex never runs; the text "
    "after a hunk header's second @@ never reaches orch (staged_raw prints no hunk, #565)",
    "funcname": "as xfuncname",
    "wordregex": "only --word-diff, which no operation uses",
}


def diff_tolerated(key: str) -> bool:
    """Whether a ``diff.*`` key of a repository's own config is on :data:`DIFF_TOLERATED`/`DIFF_DRIVER_TOLERATED`."""
    section, sub, name = _split(key)
    if section != "diff" or sub == "":  # driver "" (selected by an empty ``diff=`` attribute): refused (R7-L1)
        return False
    return name in (DIFF_TOLERATED if sub is None else DIFF_DRIVER_TOLERATED)


_HUNK = re.compile(r"(?m)^(@@ -[0-9]+(?:,[0-9]+)? \+[0-9]+(?:,[0-9]+)? @@).*$")


def plain_hunk_headers(patch: str) -> str:
    """``patch`` with the function context after each hunk header's second ``@@`` removed: a repository's
    ``diff.<driver>.xfuncname``/``funcname`` choose that text, and a person reviewing the diff must not read it as
    orch's. Only hunk headers start with ``@@`` (content lines start with `` ``, ``+``, ``-`` or ``\\``)."""
    return _HUNK.sub(r"\1", patch)


def gate(repo: str, own: Mapping[str, str], *, timeout: float = GATE_SECONDS) -> list[tuple[str, str, str]]:
    """Refuse (:class:`Unsafe`) to let git near ``repo`` when its own configuration sets a key outside the allow-list
    (:func:`exec_risk`, :data:`INVARIANT`) or its config files could be changed by another account
    (:func:`config_files`). Returns the local and worktree entries it checked."""
    if not repo.startswith("/") or "\0" in repo:
        raise Unsafe("the repository path is not absolute")
    git_version(own, timeout=timeout)
    config_files(repo, own, timeout=timeout)
    # ``diff.*`` (orch-52, #561): its own closed allow-list, DIFF_TOLERATED. Those keys are tolerated (git-lfs and
    # editors set them), neutralised, not trusted: every diff operation of OPS pins what each one could change, as
    # written next to it, and no other operation reads diff config (run_git's worktree commands run no diff). Any
    # other diff.* key is refused, so a key a future git adds is refused by default. Everything else outside the
    # allow-list is refused as before.
    entries = local_config(repo, own, timeout=timeout)
    for _scope, k, _value in entries:
        if k.lower().startswith("diff.") and not diff_tolerated(k):
            raise Unsafe(f"this repo sets {k}, which orch doesn't support yet; remove it or tell us")
    key = exec_risk(e for e in entries if not e[1].lower().startswith("diff."))
    if key is not None:
        raise Unsafe(f"the repository's own config sets {key}, which is not on orch's allow-list of repository config "
                     "(a key that could make git run a program or read config elsewhere); orch does not run git there "
                     "(remove it, or configure it globally)")  # fmt: skip
    return entries
