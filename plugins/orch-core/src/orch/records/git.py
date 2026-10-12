"""Hardened git for the records tool (D-P2-33, #254's rules as ruled by the tie-break).

**Frozen configuration.** The person's system and global configuration (credential helpers, ``url.*`` rewrites, ssh
settings) is read **once**, with includes expanded, when the repo is opened; every later call runs with
``GIT_CONFIG_NOSYSTEM=1``, ``GIT_CONFIG_GLOBAL=/dev/null`` and those entries passed as command-scope configuration, so
an edit of ``~/.gitconfig`` between the check and the push changes nothing. The repo's own (local and worktree)
configuration is checked against a deny-list (:func:`check_local`) and its digest is checked again right before the
push.

**One destination.** The effective push URL is resolved once by git itself (``git remote get-url --push`` under the
frozen configuration: ``pushurl``, ``insteadOf`` and ``pushInsteadOf`` applied) and must be exactly one URL. The
network calls (``ls-remote`` and ``push``) then run with that literal URL and with every ``url.*`` entry removed from
the frozen configuration, so nothing rewrites it again: the export check, ``ls-remote`` and ``push`` see one string.

Every call: argv only, no shell, stdin closed unless input is given, ``LANG=C``, ``GIT_TERMINAL_PROMPT=0``,
``GIT_NO_REPLACE_OBJECTS=1``, ``GIT_ALLOW_PROTOCOL`` (https and ssh), hooks off (``core.hooksPath=/dev/null``),
``core.fsmonitor=false``, ssh in batch mode when no ssh command is configured, and a timeout (10 s local, 20 s network).
"""

from __future__ import annotations

import hashlib
import os
import re
import subprocess
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from . import RecordsError

__all__ = ["ALLOWED_PROTOCOLS", "EMPTY_TREE", "Git", "check_local", "open_repo"]

ALLOWED_PROTOCOLS = "https:ssh"
EMPTY_TREE = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"
LOCAL_TIMEOUT = 10.0
NETWORK_TIMEOUT = 20.0
_HARDEN = (
    "-c", "core.hooksPath=/dev/null",
    "-c", "core.fsmonitor=false",
    "-c", "core.quotePath=false",
    "-c", "gc.auto=0",
    "-c", "maintenance.auto=false",
)  # fmt: skip
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
        "promisor", "partialclonefilter",
    }
)  # fmt: skip
_SCP = re.compile(r"^[A-Za-z0-9._~+-]+@[A-Za-z0-9.-]+:[^:].*$|^[A-Za-z0-9.-]+:[^:/].*$")
_STATE_FILES = ("MERGE_HEAD", "REBASE_HEAD", "CHERRY_PICK_HEAD", "REVERT_HEAD", "BISECT_LOG", "rebase-merge",
                "rebase-apply")  # fmt: skip


def _split_key(key: str) -> tuple[str, str | None, str]:
    section, _, rest = key.partition(".")
    sub, _, name = rest.rpartition(".")
    return section.lower(), (sub or None), name.lower()


def check_local(entries: Sequence[tuple[str, str, str]], remote: str | None) -> None:
    """Refuse a local or worktree configuration that could redirect, rewrite or execute (``records.unsafe_config``,
    naming the key). ``entries`` are (scope, key, value)."""
    for scope, key, value in entries:
        if scope not in ("local", "worktree"):
            continue
        section, sub, name = _split_key(key)
        full = f"{section}.{name}" if sub is None else None
        if section in _DENY_SECTIONS or (full is not None and full in _DENY_KEYS):
            raise RecordsError("records.unsafe_config", f"the records repo's own config sets {key}: push by hand")
        if section == "core" and name == "bare" and value.strip().lower() not in ("false", "no", "off", "0"):
            raise RecordsError("records.unsafe_config", f"the records repo's own config sets {key}")
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

    # -- running
    def env(self, *, network: bool = False, index: Path | None = None, rewrites: bool = True) -> dict[str, str]:
        entries = [(k, v) for k, v in self.frozen if rewrites or _split_key(k)[0] != "url"]
        env = dict(self.base_env)
        env.update(
            GIT_DIR=str(self.git_dir),
            GIT_WORK_TREE=str(self.root),
            GIT_CONFIG_NOSYSTEM="1",
            GIT_CONFIG_GLOBAL=os.devnull,
            GIT_CONFIG_COUNT=str(len(entries)),
        )
        for i, (k, v) in enumerate(entries):
            env[f"GIT_CONFIG_KEY_{i}"] = k
            env[f"GIT_CONFIG_VALUE_{i}"] = v
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
        rewrites: bool = True,
        check: bool = True,
        timeout: float | None = None,
    ) -> subprocess.CompletedProcess[bytes]:
        argv = ["git", *_HARDEN, *args]
        try:
            res = subprocess.run(
                argv,
                input=input,
                stdin=None if input is not None else subprocess.DEVNULL,
                capture_output=True,
                env=self.env(network=network, index=index, rewrites=rewrites),
                cwd=self.root,
                timeout=timeout or (NETWORK_TIMEOUT if network else LOCAL_TIMEOUT),
                check=False,
            )
        except subprocess.TimeoutExpired:
            raise RecordsError("records.git_failed", f"git {args[0]} timed out") from None
        if check and res.returncode != 0:
            err = res.stderr.decode(errors="replace").strip().splitlines()
            raise RecordsError("records.git_failed", f"git {args[0]} failed: {(err or ['?'])[-1][:300]}")
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
        GIT_ALLOW_PROTOCOL=protocols,
        GIT_ATTR_NOSYSTEM="1",
        GIT_OPTIONAL_LOCKS="0",
    )
    return base


def git_dir_of(root: Path) -> Path:
    """The records repo's git dir: ``<root>/.git`` as a directory, or a ``.git`` file naming a git dir inside the root.
    Anything else is ``records.not_dedicated``."""
    dot = root / ".git"
    if os.path.islink(dot):
        raise RecordsError("records.not_dedicated", f"{dot} is a symlink")
    if dot.is_dir():
        return dot
    if dot.is_file():
        text = dot.read_text(errors="replace").strip()
        if text.startswith("gitdir:"):
            target = Path(text[len("gitdir:") :].strip())
            target = (root / target) if not target.is_absolute() else target
            real, base = os.path.realpath(target), os.path.realpath(root)
            if os.path.commonpath([real, base]) == base and Path(real).is_dir():
                return Path(real)
        raise RecordsError("records.not_dedicated", f"{dot} names a git dir outside the record root")
    raise RecordsError(
        "records.not_dedicated",
        f"{root} is not the top of its own git repo",
        hint="orch records init creates a dedicated records repo there",
    )


def open_repo(
    root: Path, *, env: Mapping[str, str] | None = None, protocols: str = ALLOWED_PROTOCOLS, remote: str | None = None
) -> Git:
    """The dedicated records repo at ``root`` with its configuration frozen and checked."""
    root = Path(os.path.realpath(root))
    git_dir = git_dir_of(root)
    base = _base_env(os.environ if env is None else env, protocols)
    repo = Git(root, git_dir, protocols, base, [])
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
    repo.frozen = [
        (k, v)
        for scope, k, v in _parse_config(probe.stdout)
        if scope not in ("local", "worktree", "command") and _split_key(k)[0] not in ("include", "includeif")
    ]
    top = repo.out("rev-parse", "--show-toplevel")
    if os.path.realpath(top) != str(root):
        raise RecordsError("records.not_dedicated", f"{root} is not the top of its records repo")
    fmt = repo.run("rev-parse", "--show-object-format", check=False).stdout.decode().strip() or "sha1"
    if fmt != "sha1":
        raise RecordsError("records.unsupported", f"object format {fmt} is not supported by the records tool")
    return repo
