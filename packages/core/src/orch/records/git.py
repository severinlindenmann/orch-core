"""Hardened git for the records tool (D-P2-33, #254's rules as ruled by the tie-break).

**Frozen configuration.** The person's system and global configuration is read **once**, with includes expanded,
when the repo is opened; every later call runs with ``GIT_CONFIG_NOSYSTEM=1``, ``GIT_CONFIG_GLOBAL=/dev/null`` and an
**allow-list** of those entries passed as command-scope configuration (:func:`person_config`, the one place, #541), so
an edit of ``~/.gitconfig`` between the check and the push changes nothing, and nothing else of it reaches git: a local
call keeps ``user.name``/``user.email`` and the ``url.<base>.insteadOf``/``pushInsteadOf`` rewrites (they resolve the
push URL, which the export check then sees), a network call only authentication and transport settings
(``credential.*``, ``http.*``, ``ssh.*``, ``core.sshCommand``; the helper and the ssh command name programs by design).
No filter, ``core.attributesFile``, ``diff.external``, ``core.alternateRefsCommand``, pager, editor, alias, gpg or
encoding setting of the person's reaches the records tool's commits or its fast-forward checks.

The repo's own (local and worktree) configuration is checked against the shared allow-list of
:mod:`orch.gitsafe` and then a stricter deny-list (:func:`check_local`), and its digest is checked again right before
the push.

**One destination.** The effective push URL is resolved once by git itself (``git remote get-url --push`` under the
frozen configuration: ``pushurl``, ``insteadOf`` and ``pushInsteadOf`` applied) and must be exactly one URL. The
network calls (``ls-remote`` and ``push``) then run with that literal URL from a throwaway, config-free bare repo
(:meth:`Git.transport`, the records repo's objects as alternates) that keeps only authentication and transport
settings of the frozen configuration: no ``url.*``, no ``remote.*``, no ``.git/config`` of the records repo is read
by them, so nothing rewrites the URL again and the push does not re-read the checked config (Opus #430 findings 1,
3). The export check, ``ls-remote`` and ``push`` see one string. ``core.commitGraph`` is off everywhere, the
alternates included (finding 2).

Every call: argv only, no shell, stdin closed unless input is given, ``LANG=C``, ``GIT_TERMINAL_PROMPT=0``,
``GIT_NO_REPLACE_OBJECTS=1``, ``GIT_ALLOW_PROTOCOL`` (https and ssh), hooks off (``core.hooksPath=/dev/null``),
``core.fsmonitor=false``, ``core.attributesFile=/dev/null``, ``i18n.commitEncoding=UTF-8``, ssh in batch mode
when no ssh command is configured, and a timeout (10 s local, 20 s network).
"""

from __future__ import annotations

import contextlib
import hashlib
import os
import re
import stat
import subprocess
import tempfile
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from orch import gitsafe

from . import RecordsError

__all__ = [
    "ALLOWED_PROTOCOLS", "HARDEN", "EMPTY_TREE", "Git", "check_local", "open_repo", "person_config", "redact",
    "url_problem",
]  # fmt: skip

ALLOWED_PROTOCOLS = "https:ssh"
_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
EMPTY_TREE = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"
LOCAL_TIMEOUT = 10.0
NETWORK_TIMEOUT = 20.0
_HARDEN = (
    "-c", "core.hooksPath=/dev/null",
    "-c", "core.fsmonitor=false",
    "-c", "core.quotePath=false",
    "-c", "gc.auto=0",
    "-c", "maintenance.auto=false",
    "-c", "core.commitGraph=false",  # a forged commit-graph could lie about ancestry (Opus #430 finding 2)
    "-c", "fetch.writeCommitGraph=false",
    # core.attributesFile=/dev/null (not even git's default $XDG_CONFIG_HOME/git/attributes) and
    # i18n.commitEncoding=UTF-8 (the repo's own config may set it, it runs nothing: commits stay records-only), #541
    *gitsafe.PINNED,
)  # fmt: skip
HARDEN = _HARDEN
#: the only sections of the person's frozen configuration a network call keeps: authentication and transport
#: settings. Never ``url.*`` or ``remote.*`` (they would redirect the literal URL, Opus #430 finding 1).
_NETWORK_SECTIONS = frozenset({"credential", "http", "ssh"})
_NETWORK_KEYS = frozenset({"core.sshcommand"})
#: the only keys of the person's frozen configuration a local call keeps (#541). ``user.*`` is inert data (a records
#: commit sets ``GIT_AUTHOR_*``/``GIT_COMMITTER_*``); the ``url.<base>.*`` rewrites below only resolve the push URL.
_LOCAL_KEYS = frozenset({"user.name", "user.email"})
_LOCAL_URL_NAMES = frozenset({"insteadof", "pushinsteadof"})
#: sections the repo's own configuration may not set at all (#254 rounds 3 and 4, D-P2-33)
_DENY_SECTIONS = frozenset({"url", "http", "credential", "protocol", "filter", "include", "includeif", "lfs"})
_DENY_KEYS = frozenset(
    {
        "core.sshcommand", "core.askpass", "core.gitproxy", "core.fsmonitor", "core.hookspath", "core.worktree",
        "core.alternaterefscommand",
    }
)  # fmt: skip
#: keys of the remote in use the repo's configuration may set (#254 round 4's harmless list, plus url/pushurl/fetch)
_REMOTE_OK = frozenset(
    {
        "url", "pushurl", "fetch", "gh-resolved", "prune", "prunetags", "tagopt", "skipdefaultupdate", "skipfetchall",
    }
)  # fmt: skip
_SCP = re.compile(r"^[A-Za-z0-9._~+-]+@[A-Za-z0-9.-]+:[^:].*$|^[A-Za-z0-9.-]+:[^:].*$")
_STATE_FILES = ("MERGE_HEAD", "REBASE_HEAD", "CHERRY_PICK_HEAD", "REVERT_HEAD", "BISECT_LOG", "rebase-merge",
                "rebase-apply")  # fmt: skip


_USERINFO = re.compile(r"(?i)([a-z][a-z0-9+.-]*://)[^/@\s]*@")


#: an absolute local path in a message (not the path part of a URL: that follows a host name or ``//``)
_LOCAL_PATH = re.compile(r"(?<![\w:/.~-])/[^\s'\"]+")


def _clock() -> float:
    from orch import records

    return records.MONOTONIC()


def redact(text: str) -> str:
    """Text for an error, the output, the log or the host's audit: no ``user:password@`` of a URL, no local absolute
    path (``<path>``), no control characters."""
    text = _USERINFO.sub(r"\1***@", text)
    text = _LOCAL_PATH.sub("<path>", text)
    return "".join(ch if ch.isprintable() else "?" for ch in text)


def url_problem(url: str) -> str | None:
    """Why a URL may not be a records destination (stored in the mark and the audit, printed): credentials, or a
    query string or fragment (a token can hide there), or a control character. ``None`` when it may."""
    if any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in url):
        return "it has a control character"
    if has_credentials(url):
        return "it carries credentials; use a credential helper"
    if "?" in url or "#" in url:
        return "it has a query string or fragment"
    return None


def has_credentials(url: str) -> bool:
    """A URL that may carry a credential: any userinfo on http(s) (``https://token@host``, ``https://u:p@host``), or
    a ``user:secret@`` on any scheme. ssh's plain ``git@host`` user stays allowed. Refused: such a URL would reach the
    output, the host's mark and its audit list."""
    m = re.match(r"^([A-Za-z][A-Za-z0-9+.-]*)://([^/?#]*)@", url)
    if not m:
        return False
    return m.group(1).lower() in ("http", "https") or ":" in m.group(2)


def _split_key(key: str) -> tuple[str, str | None, str]:
    section, _, rest = key.partition(".")
    sub, _, name = rest.rpartition(".")
    return section.lower(), (sub or None), name.lower()


def person_config(entries: Sequence[tuple[str, str]], *, network: bool) -> list[tuple[str, str]]:
    """The one place that decides which of the person's (system and global) configuration entries a records git call
    sees (#541): an allow-list, never a deny-list, so a key a future git adds stays out. ``network``: a call in the
    transport repo (only authentication and transport settings); otherwise a local call (``user.name``,
    ``user.email``, ``url.<base>.insteadOf`` and ``url.<base>.pushInsteadOf``)."""
    out: list[tuple[str, str]] = []
    for key, value in entries:
        section, sub, name = _split_key(key)
        if network:
            ok = section in _NETWORK_SECTIONS or key.lower() in _NETWORK_KEYS
        elif section == "url":
            ok = sub is not None and name in _LOCAL_URL_NAMES
        else:
            ok = sub is None and f"{section}.{name}" in _LOCAL_KEYS
        if ok:
            out.append((key, value))
    return out


def check_local(entries: Sequence[tuple[str, str, str]], remote: str | None) -> None:
    """Refuse a local or worktree configuration that could redirect, rewrite or execute (``records.unsafe_config``,
    naming the key). ``entries`` are (scope, key, value). The shared allow-list of :mod:`orch.gitsafe` (only keys that
    cannot make git execute a program or read config elsewhere; config-based ``hook.*`` included, Opus #439 D1)
    applies first; the records deny-list below is stricter (redirects, network)."""
    from orch.gitsafe import exec_risk

    risky = exec_risk(entries)
    if risky is not None:
        raise RecordsError("records.unsafe_config", f"the records repo's own config sets {risky}: push by hand")
    for scope, key, value in entries:
        if scope not in ("local", "worktree"):
            continue
        section, sub, name = _split_key(key)
        full = f"{section}.{name}" if sub is None else None
        if section in _DENY_SECTIONS or (full is not None and full in _DENY_KEYS):
            raise RecordsError("records.unsafe_config", f"the records repo's own config sets {key}: push by hand")
        if (section == "extensions" and full == "extensions.partialclone") or (
            section == "remote" and name in ("promisor", "partialclonefilter")
        ):  # a partial clone could lazy-fetch behind a read, whatever the git version (Opus #454 I5)
            raise RecordsError(
                "records.unsafe_config", f"the records repo's own config sets {key}: a partial clone is not supported"
            )
        if section == "core" and name == "bare" and value.strip().lower() not in ("false", "no", "off", "0"):
            raise RecordsError("records.unsafe_config", f"the records repo's own config sets {key}")
        if section == "remote" and remote is not None and sub != remote:
            # git applies remote."<url>".* even to a push to a literal URL: only the remote in use may be configured
            raise RecordsError("records.unsafe_config", f"the records repo's own config sets {key}: push by hand")
        if section == "remote" and remote is not None and sub == remote and name not in _REMOTE_OK:
            raise RecordsError("records.unsafe_config", f"the records repo's own config sets {key}: push by hand")


def url_allowed(url: str, protocols: str) -> bool:
    allowed = set(protocols.split(":"))
    if "::" in url.split("/", 1)[0]:  # a transport helper (ext::, fd::, ...)
        return False
    m = re.match(r"^([A-Za-z][A-Za-z0-9+.-]*)://", url)
    if m:
        scheme = m.group(1).lower()
        scheme = {"git+ssh": "ssh", "ssh+git": "ssh"}.get(scheme, scheme)
        return scheme in allowed
    if _SCP.match(url) and not url.startswith(("/", ".")):
        return "ssh" in allowed
    return "file" in allowed  # a local path


@dataclass
class Git:
    """A records repo: its work tree (the record root), its git dir and the frozen configuration."""

    root: Path
    git_dir: Path
    protocols: str = ALLOWED_PROTOCOLS
    base_env: dict[str, str] = field(default_factory=dict)
    frozen: list[tuple[str, str]] = field(default_factory=list)
    local_digest: str = ""
    local: list[tuple[str, str, str]] = field(default_factory=list)
    #: a throwaway transport repo (:meth:`transport`): bare, no work tree, no config of the records repo
    bare: bool = False
    #: the run's deadline (``time.monotonic``): every call gets at most what is left (60 s per run, D-P2-33)
    deadline: float | None = None

    def remaining(self, cap: float) -> float:
        """At most ``cap`` seconds, and never past the run's deadline (``records.budget`` once it is spent)."""
        if self.deadline is None:
            return cap
        left = self.deadline - _clock()
        if left <= 0.05:
            raise RecordsError("records.budget", "the records run used its 60 s budget; try again")
        return min(cap, left)

    @contextlib.contextmanager
    def transport(self) -> Iterator[Git]:
        """A throwaway, config-free bare repo for the network calls (``ls-remote``, ``push``): a private 0700 temp
        dir whose ``objects/info/alternates`` names the records repo's objects. No config of the records repo (no
        ``remote.*``, no ``branches/`` or ``remotes/``) reaches the network calls, and of the person's frozen
        configuration only authentication and transport settings (``credential.*``, ``http.*``, ``ssh.*``,
        ``core.sshCommand``) do: the URL checked is the URL pushed to."""
        with tempfile.TemporaryDirectory(prefix="orch-records-net-") as d:
            os.chmod(d, 0o700)
            net = Path(d) / "t.git"
            subprocess.run(
                ["git", "init", "-q", "--bare", "--template=", str(net)],
                stdin=subprocess.DEVNULL,
                capture_output=True,
                env={**self.base_env, "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull},
                timeout=LOCAL_TIMEOUT,
                check=True,
            )
            info = net / "objects" / "info"
            info.mkdir(parents=True, exist_ok=True)
            fd = os.open(info / "alternates", os.O_WRONLY | os.O_CREAT | os.O_EXCL | _NOFOLLOW, 0o600)
            with os.fdopen(fd, "w") as fh:
                fh.write(os.path.realpath(self.git_dir / "objects") + "\n")
            frozen = person_config(self.frozen, network=True)
            yield Git(net, net, self.protocols, dict(self.base_env), frozen, bare=True, deadline=self.deadline)

    # -- running
    def env(self, *, network: bool = False, index: Path | None = None) -> dict[str, str]:
        if network and not self.bare:
            raise RecordsError("internal", "a network call runs only in the transport repo")
        # every call in the transport repo, network or not, sees the transport subset only (never ``url.*``)
        entries = person_config(self.frozen, network=network or self.bare)
        env = dict(self.base_env)
        env["GIT_DIR"] = str(self.git_dir)
        env.update(gitsafe.config_env(entries))  # the shared mechanism of orch.gitsafe (#546), records' own lists
        if not self.bare:
            env["GIT_WORK_TREE"] = str(self.root)
        if index is not None:
            env["GIT_INDEX_FILE"] = str(index)
        if network and not any(k.lower() == "core.sshcommand" for k, _ in entries):
            env["GIT_SSH_COMMAND"] = "ssh -o BatchMode=yes"
        return env

    def run(
        self,
        *args: str,
        input: bytes | None = None,
        network: bool = False,
        index: Path | None = None,
        check: bool = True,
        timeout: float | None = None,
    ) -> subprocess.CompletedProcess[bytes]:
        argv = ["git", *_HARDEN, *args]
        # Out of #565's scope (the gate-then-spawn race that moved direct-mode reads to orch.gitread; excluded by
        # orch-07): this is the person's dedicated records repository, run by the person-side tool, not a linked
        # working copy an agent works in. Its local config is checked by check_local and its digest again right
        # before the push (#430); no command here checks out, runs status or reads a work-tree file through git (the
        # blobs come from bytes orch read itself, the index is a temporary one built from empty), so no clean or
        # smudge filter applies. It needs git itself (fast-import, commit-tree, ls-remote, push), which the reader
        # cannot do.
        try:
            res = subprocess.run(
                argv,
                input=input,
                stdin=None if input is not None else subprocess.DEVNULL,
                capture_output=True,
                env=self.env(network=network, index=index),
                cwd=self.git_dir if self.bare else self.root,
                timeout=self.remaining(timeout or (NETWORK_TIMEOUT if network else LOCAL_TIMEOUT)),
                check=False,
            )
        except subprocess.TimeoutExpired:
            if self.deadline is not None and _clock() >= self.deadline - 0.1:
                raise RecordsError("records.budget", "the records run used its 60 s budget; try again") from None
            raise RecordsError("records.git_failed", f"git {args[0]} timed out") from None
        if check and res.returncode != 0:
            err = res.stderr.decode(errors="replace").strip().splitlines()
            raise RecordsError("records.git_failed", f"git {args[0]} failed: {redact((err or ['?'])[-1][:300])}")
        return res

    def out(self, *args: str, **kw: object) -> str:
        return self.run(*args, **kw).stdout.decode().strip()  # type: ignore[arg-type]

    # -- state
    def read_local(self) -> tuple[list[tuple[str, str, str]], str]:
        """The repo's own configuration entries (local and worktree scope, includes listed as keys) and their
        digest."""
        res = self.run("config", "--list", "--show-scope", "-z", check=False)
        entries = [e for e in _parse_config(res.stdout) if e[0] in ("local", "worktree")]
        digest = hashlib.sha256(repr(entries).encode()).hexdigest()
        return entries, digest

    def recheck_local(self) -> None:
        """The repo's own configuration is the one that was checked (right before a push)."""
        _entries, digest = self.read_local()
        if digest != self.local_digest:
            raise RecordsError("records.unsafe_config", "the records repo's config changed during this run")

    def branch(self) -> str:
        """The branch HEAD names; detached HEAD or a merge, rebase or cherry-pick in progress is
        ``records.git_state``."""
        for name in _STATE_FILES:
            if os.path.lexists(self.git_dir / name):
                raise RecordsError("records.git_state", f"{name} exists: finish or abort it first")
        res = self.run("symbolic-ref", "-q", "HEAD", check=False)
        ref = res.stdout.decode().strip()
        if res.returncode != 0 or not ref.startswith("refs/heads/"):
            raise RecordsError("records.git_state", "HEAD is detached: check out the records branch")
        return ref[len("refs/heads/") :]

    def resolve(self, ref: str) -> str | None:
        res = self.run("rev-parse", "-q", "--verify", f"{ref}^{{commit}}", check=False)
        return res.stdout.decode().strip() or None if res.returncode == 0 else None

    def no_grafts(self) -> None:
        """Replace objects are off (``GIT_NO_REPLACE_OBJECTS``); grafts and a shallow history are refused."""
        for name in ("info/grafts", "shallow"):
            if os.path.lexists(self.git_dir / name):
                raise RecordsError("records.history_invalid", f"the records repo has {name}: history cannot be checked")


def _parse_config(raw: bytes) -> list[tuple[str, str, str]]:
    """``git config --list --show-scope -z``: ``scope NUL key LF value NUL`` per entry."""
    parts = raw.split(b"\0")
    out: list[tuple[str, str, str]] = []
    i = 0
    while i + 1 < len(parts):
        scope = parts[i].decode(errors="surrogateescape")
        key, _, value = parts[i + 1].decode(errors="surrogateescape").partition("\n")
        out.append((scope, key, value))
        i += 2
    return out


def _base_env(env: Mapping[str, str], protocols: str) -> dict[str, str]:
    base = {k: env[k] for k in ("HOME", "PATH", "SSH_AUTH_SOCK", "XDG_CONFIG_HOME") if env.get(k)}
    base.setdefault("PATH", "/usr/bin:/bin:/usr/sbin:/sbin")
    base.update(
        LANG="C",
        LC_ALL="C",
        GIT_TERMINAL_PROMPT="0",
        GIT_NO_REPLACE_OBJECTS="1",
        GIT_GRAFT_FILE=os.devnull,  # grafts are refused anyway (no_grafts); defence in depth (#561 final delta, MC)
        GIT_ALLOW_PROTOCOL=protocols,
        GIT_ATTR_NOSYSTEM="1",
        GIT_OPTIONAL_LOCKS="0",
        GIT_NO_LAZY_FETCH="1",  # a partial clone never fetches a missing object behind a read (Opus #454 L2)
    )
    return base


def pinned_root(root: Path) -> Path:
    """The record root as given, absolute, with no symlinked component anywhere on its path (checked component by
    component with ``lstat``): every later path under it is the one that was checked."""
    path = Path(os.path.abspath(root))
    cur = Path(path.anchor)
    for part in path.parts[1:]:
        cur = cur / part
        try:
            st = os.lstat(cur)
        except FileNotFoundError:
            raise RecordsError("records.not_dedicated", "the record root does not exist") from None
        if stat.S_ISLNK(st.st_mode):
            raise RecordsError("records.unsafe_tree", "the record root's path passes through a symlink")
    return path


def git_dir_of(root: Path) -> Path:
    """The records repo's git dir: ``<root>/.git`` as a directory, or a ``.git`` file naming a git dir inside the root.
    Anything else is ``records.not_dedicated``."""
    dot = root / ".git"
    if os.path.islink(dot):
        raise RecordsError("records.not_dedicated", "the record root's .git is a symlink")
    if dot.is_dir():
        return dot
    if dot.is_file():
        fd = os.open(dot, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        try:
            text = os.read(fd, 4096).decode(errors="replace").strip()
        finally:
            os.close(fd)
        if text.startswith("gitdir:"):
            target = Path(text[len("gitdir:") :].strip())
            target = (root / target) if not target.is_absolute() else target
            try:  # a name too long or otherwise unusable is no git dir (Python 3.11 raises here)
                real, base = os.path.realpath(target), os.path.realpath(root)
                if os.path.commonpath([real, base]) == base and Path(real).is_dir():
                    return Path(real)
            except (OSError, ValueError):
                pass
        raise RecordsError("records.not_dedicated", "the record root's .git file names a git dir outside it")
    raise RecordsError(
        "records.not_dedicated",
        "the record root is not the top of its own git repo",
        hint="orch records init creates a dedicated records repo there",
    )


def open_repo(
    root: Path,
    *,
    env: Mapping[str, str] | None = None,
    protocols: str = ALLOWED_PROTOCOLS,
    remote: str | None = None,
    deadline: float | None = None,
) -> Git:
    """The dedicated records repo at ``root`` with its configuration frozen and checked."""
    root = pinned_root(root)
    git_dir = git_dir_of(root)
    base = _base_env(os.environ if env is None else env, protocols)
    repo = Git(root, git_dir, protocols, base, [], deadline=deadline)
    repo.local, repo.local_digest = repo.read_local()  # the repo's own config first, without following includes
    check_local(repo.local, remote)
    probe = subprocess.run(  # the person's real configuration, read once
        ["git", "config", "--list", "--show-scope", "--includes", "-z"],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        env={**base, "GIT_DIR": str(git_dir), "GIT_WORK_TREE": str(root)},
        cwd=root,
        timeout=LOCAL_TIMEOUT,
        check=False,
    )
    if probe.returncode != 0:
        raise RecordsError("records.git_failed", "git config --list failed in the records repo")
    person = [(k, v) for scope, k, v in _parse_config(probe.stdout) if scope not in ("local", "worktree", "command")]
    allowed = {*person_config(person, network=False), *person_config(person, network=True)}
    repo.frozen = [e for e in person if e in allowed]  # nothing outside the allow-lists is even kept
    top = repo.out("rev-parse", "--show-toplevel")
    if os.path.realpath(top) != str(root):
        raise RecordsError("records.not_dedicated", "the record root is not the top of its records repo")
    fmt = repo.run("rev-parse", "--show-object-format", check=False).stdout.decode().strip() or "sha1"
    if fmt != "sha1":
        raise RecordsError("records.unsupported", f"object format {fmt} is not supported by the records tool")
    return repo
