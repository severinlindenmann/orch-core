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

**Isolation: the second half of the invariant (#541, #546).** *No git invocation by orch reads configuration the
person or a repo controls, except through an explicit allow-list.* The repo's half is :func:`gate` (above). The
person's half is ONE mechanism here, used by every read-only caller (the gate itself, :func:`orch.store.observe.git`,
``orch.ops.health``'s staged check, :func:`orch.runner.wtgit.read_git`) and, for its config part, by the records tool:

* :func:`isolated_env`: the whole environment, built from scratch (``HOME``, ``PATH``, ``TMPDIR``, ``USER``,
  ``LOGNAME``, ``XDG_CONFIG_HOME``; never a ``GIT_*`` variable, token or ``ORCH_*`` of the caller), with
  ``GIT_CONFIG_NOSYSTEM=1``, ``GIT_CONFIG_GLOBAL=/dev/null``, no system attributes, no replace objects, no prompt, no
  network protocol, no lazy fetch, ``GIT_PAGER=cat`` and ``GIT_EDITOR=true``;
* :func:`config_env`: the person's configuration an operation allows, passed explicitly as command-scope entries
  (``GIT_CONFIG_COUNT``), never by reading their files; :func:`person_entries` reads those files once (reading
  configuration executes nothing) and keeps only the operation's own allow-list (``section.name``, no subsection):
  ``status`` keeps :data:`STATUS_KEYS` (``core.excludesFile``, ``core.autocrlf``, ``core.eol``: the person's own idea
  of "clean"), every other operation nothing; the records tool keeps its lists in
  :func:`orch.records.git.person_config`;
* :func:`command`: **a closed set of operations** (:data:`OPS`, Codex P1 on #561), never free argv. Each has fixed
  arguments and at most one validated operand (a revision: ``HEAD`` or ``refs/heads/<name>``; an index path, given
  as the object name ``:0:<path>``), so no caller can add a global option (``-c``, ``-C``, ``--git-dir``,
  ``--work-tree``, ``--exec-path``, ``--namespace``, ``-p``, ``--config-env``), an option or a subcommand; no
  operation can reach the network. It runs :func:`gate` before every operation (and :func:`submodules` before
  ``status``, which also never opens a submodule) and builds the argv: :data:`HARDEN` (:data:`NEUTRAL` plus
  :data:`PINNED`: ``core.attributesFile=/dev/null``, git's default ``$XDG_CONFIG_HOME/git/attributes`` is not read,
  and ``i18n.commitEncoding=UTF-8``), ``--no-pager``, ``--no-optional-locks``, ``-C <repo>``, and ``--no-ext-diff
  --no-textconv`` on ``diff``.

**Every git invocation site** and how it meets the invariant:

======================================================  ===========================================================
Site                                                    Isolation
======================================================  ===========================================================
``gitsafe._run`` (the gate's own reads)                 its fixed reads, :func:`isolated_env`, no person keys
``store/observe.git`` (``head``, ``symbolic_head``,     :func:`command` (operations of :data:`OPS` only, gate
``toplevel``, ``porcelain``, ``dirty``; ``ops/caps.Git``  every time); person keys only for ``status``; the
and ``hostclient`` in direct mode)                      identity's ``remote.origin.url`` comes from
                                                        :func:`local_config`, the gate's own listing (#546).
                                                        ``ops/caps.Git.run`` (free argv) is removed
``ops/health._git`` (``check --staged``: ``staged_raw``,  :func:`gate` once per check for its message, then
``staged_blob``)                                        :func:`command` (gate again), through ``caps.exec`` (#546)
``runner/wtgit.read_git``, ``tree_dirty``,              :func:`command`: ``commit``, ``status``, ``staged_raw``,
``worktree_cli`` revisions                              ``index_flags``
dashboard dev host ``devhost/reads.diff`` (through      :func:`command`: ``commit``, ``ref``, ``merge_base``,
``store/observe.git``)                                  ``parent``, ``diff_numstat``, ``diff_names``, ``diff_patch``
``records/git.Git.run``, ``records/tool`` fast-import   :func:`config_env` with ``person_config``'s lists, the
                                                        records ``HARDEN`` (incl. :data:`PINNED`), ``check_local``
                                                        on top of :func:`exec_risk` (#541)
``records`` ``git init --template=`` (repo and          system and global config off, no frozen config (#541)
transport)
``records/git.open_repo`` probe                         reads configuration only (executes nothing)
``gitsafe.person_entries`` (``config --list --show-     reads the person's system and global configuration as
scope --includes -z``, ``GIT_DIR=/dev/null/none``)      data, outside any repository, keeps only the operation's
                                                        allow-list (:data:`STATUS_KEYS`); executes nothing
``gitsafe.git_version`` (``git --version``)             :func:`isolated_env`; once per ``PATH``, refuses git < 2.32
``runner/wtgit.run_git`` (``worktree add/remove/        **The one exception.** :func:`gate` on the repo (and the
prune``)                                                worktree) plus :data:`NEUTRAL`, but the person's system and
                                                        global configuration stays on **by design**: a checkout
                                                        runs their checkout filters (``git lfs install``'s smudge),
                                                        exactly as when they run ``git worktree add`` themselves
``gitread/*``                                           no git process (a pure-Python reader in a sandbox)
======================================================  ===========================================================

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
import re
import signal
import stat
import subprocess
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass

__all__ = [
    "ALLOWED", "ALLOWED_SUB", "HARDEN", "INVARIANT", "ISOLATION", "KNOWN_EXEC", "NEUTRAL", "OPS", "PINNED",
    "STATUS_KEYS", "Op", "Unsafe", "command", "config_env", "config_files", "exec_risk", "gate", "gitlinks",
    "isolated_env", "local_config", "person_entries", "submodules",
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
#: every isolated command (:func:`command`)
HARDEN = (*NEUTRAL, *PINNED)
#: subcommands that take ``--no-ext-diff --no-textconv`` (no diff program, no textconv program)
_NO_PROGRAMS = frozenset({"diff", "show", "log"})
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


def isolated_env(own: Mapping[str, str], person: Sequence[tuple[str, str]] = ()) -> dict[str, str]:
    """THE environment of every isolated git command (:data:`ISOLATION`, #541, #546): built from scratch out of
    ``own`` (:data:`_KEEP` only: never a ``GIT_*`` variable, a token or an ``ORCH_*`` variable of the caller), system
    and global configuration off, and of the person's configuration exactly ``person`` (already filtered by the
    caller's own allow-list, :func:`person_entries`), passed by :func:`config_env`."""
    env = {k: own[k] for k in _KEEP if own.get(k) and "\0" not in own[k]}
    env.setdefault("PATH", "/usr/bin:/bin:/usr/local/bin")
    env.update(
        LANG="C", LC_ALL="C", GIT_TERMINAL_PROMPT="0", GIT_OPTIONAL_LOCKS="0", GIT_ALLOW_PROTOCOL="",
        GIT_NO_LAZY_FETCH="1", GIT_ATTR_NOSYSTEM="1", GIT_NO_REPLACE_OBJECTS="1", GIT_PAGER="cat", GIT_EDITOR="true",
    )  # fmt: skip
    env.update(config_env(person))
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


def _argv(repo: str, args: Sequence[str]) -> list[str]:
    """The argv of one isolated git command against ``repo`` (absolute): :data:`HARDEN`, no pager, no optional locks,
    and ``--no-ext-diff --no-textconv`` right after ``diff``, ``show`` or ``log``. Private: ``args`` are only ever the
    gate's own fixed reads or an operation of :data:`OPS` (:func:`command`)."""
    rest = list(args)
    if rest and rest[0] in _NO_PROGRAMS:
        rest[1:1] = ["--no-ext-diff", "--no-textconv"]
    return ["git", *HARDEN, "--no-pager", "--no-optional-locks", "-C", repo, *rest]


#: the person's (system and global) configuration ``status`` keeps, so that "clean" means what it means in their own
#: terminal: their global ignore file and their line-ending conversion. Each names a file of patterns or a setting,
#: never a program. No other operation keeps any of the person's configuration.
STATUS_KEYS = frozenset({"core.excludesfile", "core.autocrlf", "core.eol"})
#: Not a key but the same idea: git's *default* global ignore file (``$XDG_CONFIG_HOME/git/ignore``, else
#: ``~/.config/git/ignore``) is still read by every isolated ``status`` (``HOME`` and ``XDG_CONFIG_HOME`` are kept).
#: It is a pattern file and runs nothing (Opus delta on #561, Info 5).


@dataclass(frozen=True)
class Op:
    """One read-only git operation: fixed arguments, some holding a slot for a validated operand (``<rev>``: a
    revision; ``<path>``: a path in the index; ``<context>``: 0 to 10 lines), the person keys it keeps, and whether
    it reads the working tree (then a populated submodule is refused first, :func:`submodules`)."""

    args: tuple[str, ...]
    person: frozenset[str] = frozenset()
    tree: bool = False

    @property
    def operands(self) -> tuple[str, ...]:
        """The kinds of the operands, in order."""
        return tuple(m for a in self.args for m in _SLOT.findall(a))


_SLOT = re.compile(r"<(rev|path|context)>")

#: The CLOSED set of git operations orch's read-only callers may run (#546, Codex P1 on #561): ``store/observe``
#: (and through it ``ops/caps.Git``, ``hostclient`` and the dashboard's dev host), ``ops/health`` and
#: ``runner/wtgit.read_git``. There is no free argv: no caller can add a global option (``-c``, ``-C``,
#: ``--git-dir``, ``--work-tree``, ``--exec-path``, ``--namespace``, ``-p``, ``--config-env``), an option of its own
#: or a subcommand; revisions are followed by ``--``. No operation can reach the network. Submodules: ``status`` and
#: every diff run with ``--ignore-submodules=all`` (git never opens one) and ``status`` refuses a populated one
#: anyway; the one exception is ``staged_raw`` (``diff --cached``, index against ``HEAD``, never opens a
#: submodule), which keeps ``=dirty`` so ``orch check --staged`` still sees a staged gitlink.
OPS: dict[str, Op] = {
    "head": Op(("rev-parse", "--verify", "-q", "HEAD^{commit}")),
    "symbolic_head": Op(("symbolic-ref", "-q", "HEAD")),
    "toplevel": Op(("rev-parse", "--show-toplevel")),
    "ref": Op(("rev-parse", "--verify", "-q", "<rev>")),
    "commit": Op(("rev-parse", "--verify", "-q", "<rev>^{commit}")),
    "parent": Op(("rev-parse", "--verify", "-q", "<rev>^1")),
    "merge_base": Op(("merge-base", "<rev>", "<rev>", "--")),
    "status": Op(("status", "--porcelain=v1", "--untracked-files=normal", "--ignore-submodules=all"),
                 person=STATUS_KEYS, tree=True),
    "index_flags": Op(("ls-files", "-v")),
    "staged_raw": Op(("diff", "--cached", "--raw", "-z", "--no-renames", "--relative", "--ignore-submodules=dirty")),
    "staged_blob": Op(("cat-file", "blob", ":0:<path>")),
    "diff_numstat": Op(("diff", "--numstat", "-M50%", "--ignore-submodules=all", "<rev>", "<rev>", "--")),
    "diff_names": Op(("diff", "--name-status", "-M50%", "--ignore-submodules=all", "<rev>", "<rev>", "--")),
    "diff_patch": Op(("diff", "-U<context>", "-M50%", "--ignore-submodules=all", "<rev>", "<rev>", "--")),
}  # fmt: skip
#: a revision operand: ``HEAD``, a full commit id or a branch ref; never a leading ``-``, ``..``, ``@{``, ``//`` or
#: a trailing ``/`` or ``.``: what git can only read as one object name
_REV = re.compile(
    r"HEAD|[0-9a-f]{40}|[0-9a-f]{64}|refs/heads/(?!.*\.\.)(?!.*@\{)(?!.*//)[A-Za-z0-9_+@][A-Za-z0-9._+@/-]{0,199}(?<![/.])"
)


def _check(kind: str, value: object) -> str:
    if not isinstance(value, str) or not value or "\0" in value or "\n" in value:
        raise Unsafe("invalid git operand")
    if kind == "rev":
        if not _REV.fullmatch(value) or value.endswith(".lock"):
            raise Unsafe("invalid revision")
    elif kind == "context":
        if not re.fullmatch(r"[0-9]|10", value):
            raise Unsafe("invalid context")
    elif value.startswith("/") or len(value) > 4096:  # a path of the index, relative to the top
        raise Unsafe("invalid path")
    return value


def _fill(op: Op, operand: str | tuple[str, ...] | None) -> list[str]:
    """``op``'s arguments with each slot replaced by its validated operand (a path only ever lands in the object name
    ``:0:<path>``: never an option, never a stage or a ``:/`` search)."""
    values = () if operand is None else (operand,) if isinstance(operand, str) else tuple(operand)
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
) -> tuple[list[str], dict[str, str]]:
    """The argv and environment of operation ``op`` of :data:`OPS` against ``repo`` (absolute), for the caller to
    spawn (``cwd="/"``, stdin closed). Runs :func:`gate` first, every time, and :func:`submodules` for an operation
    that reads the working tree; the person keys come from the operation, never from the caller. Raises
    :class:`Unsafe` (git must not run) for an unknown operation, an invalid operand or a refusal."""
    spec = OPS.get(op)
    if spec is None:
        raise Unsafe("unknown git operation")
    args = _fill(spec, operand)
    gate(repo, own, timeout=timeout)
    if spec.tree:
        submodules(repo, own, timeout=timeout)
    entries = person_entries(own, spec.person, timeout=timeout)
    return _argv(repo, args), isolated_env(own, entries)


def gitlinks(raw: bytes) -> list[str]:
    """The paths of ``staged_raw`` output (``-z``) whose old or new mode is a gitlink (``160000``)."""
    fields = raw.split(b"\0")
    out: list[str] = []
    for i in range(0, len(fields) - 1, 2):
        meta = fields[i].lstrip(b":").split()
        if len(meta) >= 2 and b"160000" in (meta[0], meta[1]):
            out.append(fields[i + 1].decode("utf-8", "surrogateescape"))
    return out


def person_entries(
    own: Mapping[str, str], allow: Iterable[str], *, timeout: float = GATE_SECONDS
) -> list[tuple[str, str]]:
    """The person's system and global configuration entries (includes expanded) whose ``section.name`` (no
    subsection, case-insensitive) is in ``allow``: the caller's own allow-list. Read with git itself, outside any
    repository (reading configuration executes nothing). ``[]`` when ``allow`` is empty or the configuration cannot be
    read (then nothing of the person's reaches git: fail closed)."""
    wanted = {a.lower() for a in allow}
    if not wanted:
        return []
    env = {k: own[k] for k in _KEEP if own.get(k) and "\0" not in own[k]}
    env.setdefault("PATH", "/usr/bin:/bin:/usr/local/bin")
    env.update(LANG="C", LC_ALL="C", GIT_TERMINAL_PROMPT="0", GIT_DIR="/dev/null/none")  # no repository at all
    try:
        proc = subprocess.Popen(  # noqa: S603 - fixed argv
            ["git", "config", "--list", "--show-scope", "--includes", "-z"],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, env=env, cwd="/",
            start_new_session=True, close_fds=True,
        )  # fmt: skip
    except OSError:
        return []
    try:
        out, _ = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        with contextlib.suppress(OSError):
            os.killpg(proc.pid, signal.SIGKILL)  # the group this call created, nothing else
        with contextlib.suppress(Exception):
            proc.communicate(timeout=5)
        return []
    if proc.returncode != 0:
        return []
    picked: list[tuple[str, str]] = []
    for scope, key, value in _parse(out):
        if scope in ("local", "worktree", "command"):
            continue
        section, sub, name = _split(key)
        if sub is None and f"{section}.{name}" in wanted:
            picked.append((key, value))
    return picked


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
    key = isolated_env(own)["PATH"]
    got = _VERSIONS.get(key)
    if got is None:
        try:
            done = subprocess.run(  # noqa: S603 - fixed argv
                ["git", "--version"], stdin=subprocess.DEVNULL, capture_output=True, env=isolated_env(own), cwd="/",
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
        raise Unsafe(f"git {got[0]}.{got[1]} is too old: orch needs git 2.32 or newer to keep its configuration out")
    return got


def gate(repo: str, own: Mapping[str, str], *, timeout: float = GATE_SECONDS) -> None:
    """Refuse (:class:`Unsafe`) to let git near ``repo`` when its own configuration sets a key outside the allow-list
    (:func:`exec_risk`, :data:`INVARIANT`) or its config files could be changed by another account
    (:func:`config_files`)."""
    if not repo.startswith("/") or "\0" in repo:
        raise Unsafe("the repository path is not absolute")
    git_version(own, timeout=timeout)
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
