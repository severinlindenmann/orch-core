"""The reader in-process, for direct mode (section 3: ``git`` in direct mode is "the same reader, in-process").

**#565: direct mode runs no git process for a read.** :class:`LocalGitReader` (one instance, :data:`READER`) is the
one in-process reader every direct-mode read goes through: repository discovery from a path, ``HEAD`` (branch and
commit), a branch's commit, the repository's identity, the working tree's cleanliness (:mod:`orch.gitread.treecheck`)
and the review diff (:func:`orch.gitread.ops.diff`). Before #565 these ran git after :func:`orch.gitsafe.gate` had
approved the repository's config, and a concurrent writer of ``.git/config`` could add a ``filter.<x>.clean`` between
the two (it ran under ``status``). Here no git starts, the repository's config is read as data (three keys, nothing
acted on), and attributes, filters, hooks and fsmonitor are never read, so there is nothing to race.

No child, no audit hook, no pin store: the pins are taken afresh on every call (direct mode has no registration).
Everything fails closed: a layout, config, extension, ref store, index or object the reader cannot parse raises
:class:`~orch.gitread.errors.Unavailable`, which callers turn into "unknown" (``None``, ``observe.unavailable``,
"diff unavailable"), never into "clean".
"""

from __future__ import annotations

import base64
import os
import stat
from collections.abc import Mapping
from typing import Any

from . import ops
from .errors import Unavailable
from .ignorepolicy import IgnorePolicy, PersonIgnore, git_bool
from .pinning import pin_repo
from .repo import Repo

__all__ = ["READER", "LocalGitReader", "diff", "discover", "observe", "person_ignore", "tree_check"]


def _roots(path: str | os.PathLike[str], roots: list[str] | None) -> tuple[str, list[str] | None]:
    """Direct mode resolves the CLI's own path (P1's ``repo_path`` does too). ``roots`` ``None``: no containment
    check (no registration in direct mode); git's layout is still checked."""
    real = os.path.realpath(os.fspath(path))
    if roots is None:
        return real, None
    rs = [os.path.realpath(r) for r in roots]
    return real, rs if real in rs else [real, *rs]


def discover(path: str | os.PathLike[str]) -> str | None:
    """The top of the working copy ``path`` is in, found without git: ``path`` resolved, then it and each parent in
    turn, the first holding a ``.git`` entry (a directory or a ``.git`` file). Like git's default
    (``GIT_DISCOVERY_ACROSS_FILESYSTEM`` off), the search stops where the file system changes. ``None`` when there
    is none. The top is not validated here: opening it (:func:`~orch.gitread.pinning.pin_repo`) checks git's layout
    and refuses anything else."""
    try:
        cur = os.path.realpath(os.fspath(path))
        dev = os.stat(cur).st_dev
    except (OSError, ValueError):
        return None
    while True:
        try:
            os.lstat(os.path.join(cur, ".git"))
            return cur
        except FileNotFoundError:
            pass
        except OSError:
            return None
        parent = os.path.dirname(cur)
        if parent == cur:
            return None
        try:
            if os.stat(parent).st_dev != dev:
                return None
        except OSError:
            return None
        cur = parent


#: the person's global ignore file is read only up to this size (larger: not read, so only ever dirtier)
EXCLUDES_MAX = 1 << 20


def _read_small(path: str, limit: int) -> bytes | None:
    """A regular file's bytes (no FIFO is ever opened for reading: ``O_NONBLOCK``, then a regular-file check), or
    ``None``."""
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_CLOEXEC", 0))
    except OSError:
        return None
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_size > limit:
            return None
        chunks, total = [], 0
        while True:
            b = os.read(fd, 65536)
            if not b:
                return b"".join(chunks)
            total += len(b)
            if total > limit:
                return None
            chunks.append(b)
    except OSError:
        return None
    finally:
        os.close(fd)


#: system config files git may read, depending on how it was built (``$(prefix)/etc/gitconfig``): checked so that an
#: ``excludesFile`` (or an include) set there never makes the reader fall back to the default ignore file (#583, M2)
SYSTEM_CONFIGS = (
    "/etc/gitconfig",
    "/usr/local/etc/gitconfig",
    "/opt/homebrew/etc/gitconfig",
    "/opt/local/etc/gitconfig",
    "/Library/Developer/CommandLineTools/usr/share/git-core/gitconfig",
    "/Applications/Xcode.app/Contents/Developer/usr/share/git-core/gitconfig",
)


class _Unsure(Exception):
    """The person's configuration cannot be read with certainty."""


def _read_person_config(path: str, facts: dict[str, Any]) -> None:
    """One config file git reads for the person: ``core.excludesFile`` (last value; ``""`` when set empty) and
    ``core.ignorecase`` into ``facts``. Raises :class:`_Unsure` when the file exists but cannot be read or parsed,
    has an ``include``/``includeIf`` section (git follows those; this reader does not) or a non-boolean
    ``ignorecase``."""
    from .config import parse

    if not os.path.lexists(path):
        return
    data = _read_small(path, EXCLUDES_MAX)
    if data is None:
        raise _Unsure(f"{path} cannot be read")
    try:
        entries = parse(data)
    except Unavailable:
        raise _Unsure(f"{path} cannot be parsed") from None
    for section, sub, key, v in entries:
        if section in ("include", "includeif"):
            raise _Unsure(f"{path} has an include")
        if section == "core" and sub is None and key == "excludesfile":
            facts["excludes"] = v if v is not None else ""
        if section == "core" and sub is None and key == "ignorecase":
            got = git_bool(v)
            if got is None:
                raise _Unsure(f"{path} sets core.ignorecase to a value that is not a boolean")
            facts["ignorecase"] = got


def person_ignore(env: Mapping[str, str]) -> PersonIgnore:
    """What git would take from the person's configuration for ignore matching, as far as this reader can be sure
    (#583 round 3, B; the decision itself is :class:`orch.gitread.ignorepolicy.IgnorePolicy`'s).

    git reads the system file (unless ``GIT_CONFIG_NOSYSTEM``), then ``$XDG_CONFIG_HOME/git/config`` and
    ``~/.gitconfig`` (or ``$GIT_CONFIG_GLOBAL``), the last value winning, includes followed. **Not known** (the policy
    then matches in both case modes and does not read the person's ignore file) when: ``HOME`` or ``XDG_CONFIG_HOME``
    is not absolute; the environment passes configuration or points elsewhere (any ``GIT_CONFIG_*`` but
    ``GIT_CONFIG_GLOBAL``, and ``GIT_CONFIG_NOSYSTEM`` unless exactly ``1``); any of those files exists but cannot be
    read or parsed, or has an include. ``ignorecase``: the last ``core.ignorecase`` any of them sets (``None``: none
    does). ``excludes``: the file git uses as ``core.excludesFile`` (its default ``$XDG_CONFIG_HOME/git/ignore`` only
    when none is set), ``None`` when none, empty, relative or unreadable (the lowest source: dropping it is only ever
    dirtier). Residual: a system config at a build prefix not in
    :data:`SYSTEM_CONFIGS`."""
    home = env.get("HOME") or ""
    if not home.startswith("/"):
        return PersonIgnore(False, why="HOME is not an absolute path")
    for k in env:
        if k.startswith("GIT_CONFIG") and k not in ("GIT_CONFIG_GLOBAL", "GIT_CONFIG_NOSYSTEM"):
            return PersonIgnore(
                False,
                why=f"{k} is set: git's configuration comes from the environment",
                env_config=any(e in env for e in ("GIT_CONFIG_COUNT", "GIT_CONFIG_PARAMETERS")),
            )
    nosystem = env.get("GIT_CONFIG_NOSYSTEM")
    if nosystem is not None and nosystem != "1":
        return PersonIgnore(False, why="GIT_CONFIG_NOSYSTEM is set to a value other than 1")
    xdg = env.get("XDG_CONFIG_HOME")
    if xdg is not None and xdg != "" and not xdg.startswith("/"):
        return PersonIgnore(False, why="XDG_CONFIG_HOME is not an absolute path")
    xdg = xdg or os.path.join(home, ".config")
    files: list[str] = [] if nosystem == "1" else list(SYSTEM_CONFIGS)
    glob_ = env.get("GIT_CONFIG_GLOBAL")
    files += [glob_] if glob_ else [os.path.join(xdg, "git", "config"), os.path.join(home, ".gitconfig")]
    facts: dict[str, Any] = {"excludes": None, "ignorecase": None}
    try:
        for cfg in files:
            if cfg != os.devnull:
                _read_person_config(cfg, facts)
    except _Unsure as exc:
        return PersonIgnore(False, why=f"the person's git config is not certain ({exc})")
    value = facts["excludes"]
    if value is None:
        path: str | None = os.path.join(xdg, "git", "ignore")
    elif value.startswith("~/"):
        path = os.path.join(home, value[2:])
    elif value.startswith("/"):
        path = value
    else:
        path = None  # empty (no global file), "~user/" or relative to git's working directory
    data = _read_small(path, EXCLUDES_MAX) if path is not None and os.path.lexists(path) else None
    return PersonIgnore(True, facts["ignorecase"], data)


#: the work one direct-mode tree check may take (the host's reader child has ``limits.WALL_SECONDS`` too)
SECONDS = 30.0


class LocalGitReader:
    """Direct mode's one git reader (#565): every method opens the repository under fresh pins, reads it with orch's
    own code and never starts a process. ``path`` may be anywhere inside a working copy: the repository is found from
    it (:func:`discover`) and read **as a whole from its top** (a subdirectory workspace sees every change of its
    repository, #561's rule). Methods raise :class:`~orch.gitread.errors.Unavailable` when the repository cannot be
    read; :mod:`orch.store.observe` turns that into ``None``."""

    def _open(self, path: str | os.PathLike[str]) -> tuple[str, Any]:
        top = discover(path)
        if top is None:
            raise Unavailable("not a git working copy (no .git here or above)")
        return top, pin_repo(top, None)

    def toplevel(self, path: str | os.PathLike[str]) -> str:
        """The top of the working copy ``path`` is in (its layout checked: a ``.git`` the reader refuses is
        :class:`Unavailable`)."""
        top, _pins = self._open(path)
        return top

    def head(self, path: str | os.PathLike[str]) -> tuple[str | None, str | None]:
        """``(branch ref, commit)`` of ``HEAD``: ``(None, id)`` when detached, ``(ref, None)`` on an unborn branch."""
        from .refs import read_head_any

        _top, pins = self._open(path)
        with Repo(pins) as repo:
            ref, detached = read_head_any(repo.gitdir, repo.hexlen)
            if ref is None:
                repo.commit(detached)  # type: ignore[arg-type]  # a commit object, as rev-parse HEAD^{commit} wants
                return None, detached
            return ref, repo.resolve_commit(ref)

    def commit(self, path: str | os.PathLike[str], ref: str) -> str | None:
        """The commit the full ref ``ref`` (``refs/heads/<name>``) names, ``None`` when there is no such ref. The
        whole ref store is read first (a ref store the reader refuses is :class:`Unavailable`)."""
        _top, pins = self._open(path)
        with Repo(pins) as repo:
            repo.refs  # noqa: B018 (checked as a whole)
            return repo.resolve_commit(ref)

    def origin_urls(self, path: str | os.PathLike[str]) -> list[str]:
        _top, pins = self._open(path)
        with Repo(pins) as repo:
            return list(repo.config.origin_urls)

    def identity(self, path: str | os.PathLike[str], name: str) -> str:
        """F1 5.7: the canonical identity from the raw ``remote.origin.url`` (one value), else ``local:<name>``; a
        repository the reader cannot open is ``local:<name>`` too (as a config listing that failed was before)."""
        from orch import canon

        try:
            urls = self.origin_urls(path)
        except Unavailable:
            urls = []
        return canon.canonical_repo_identity(urls[0] if len(urls) == 1 else "", name)

    def tree_check(
        self,
        path: str | os.PathLike[str],
        env: Mapping[str, str] | None = None,
        *,
        pins: Any = None,
        removal: bool = False,
    ) -> dict[str, Any]:
        """The raw ``tree.check`` answer (:mod:`orch.gitread.treecheck`) for the repository ``path`` is in, from its
        top: a detached ``HEAD`` is compared like a branch, and the person's global ignore file (``env``'s ``HOME``,
        :func:`person_ignore`, as :class:`~orch.gitread.ignorepolicy.IgnorePolicy` decides) is honoured. ``removal``:
        the check a worktree removal needs (no ignore rule at all, untracked directories whole with sizes, and the
        ``fingerprint`` of the untracked set). ``HEAD`` (``head``, ``head_ref``) and the tree come from this one read.
        ``pins``: read exactly these :class:`~orch.gitread.fs.RepoPins` (a registered worktree) instead of finding the
        repository from ``path`` again. Bounded by :data:`SECONDS` of work (#583: direct mode has no child wall
        clock); past it the answer is unknown."""
        from . import treecheck
        from .fs import open_pin
        from .wildmatch import Budget

        if pins is None:
            _top, pins = self._open(path)
        person = person_ignore(env if env is not None else os.environ)
        budget = Budget(SECONDS)
        with Repo(pins) as repo:
            worktree = open_pin(pins.worktree)
            try:
                if removal:
                    policy = IgnorePolicy.removal(budget)
                else:
                    policy = IgnorePolicy.status(repo, person, budget)
                return treecheck.check(repo, worktree, detached_ok=True, policy=policy, budget=budget, removal=removal)
            finally:
                worktree.close()

    def worktree_diff(
        self,
        path: str | os.PathLike[str],
        env: Mapping[str, str] | None = None,
        *,
        max_bytes: int = 262144,
        max_files: int = 200,
    ) -> dict[str, Any]:
        """:func:`orch.gitread.ops.worktree_diff` for the repository ``path`` is in (direct mode: the person's global
        ignore file is honoured, a detached ``HEAD`` is compared like a branch)."""
        from .fs import open_pin
        from .wildmatch import Budget

        _top, pins = self._open(path)
        person = person_ignore(env if env is not None else os.environ)
        budget = Budget(SECONDS)
        with Repo(pins) as repo:
            worktree = open_pin(pins.worktree)
            try:
                policy = IgnorePolicy.status(repo, person, budget)
                return ops.worktree_diff(
                    repo, worktree, max_bytes=max_bytes, max_files=max_files,
                    detached_ok=True, policy=policy, budget=budget,
                )  # fmt: skip
            finally:
                worktree.close()

    def dirty(self, path: str | os.PathLike[str], env: Mapping[str, str] | None = None) -> str:
        """What is uncommitted: ``""`` when clean, else one ``why: path`` line per change (paths escaped for
        display, :func:`orch.gitread.treejob.escape_path`) and ``and more`` past 200. Raises :class:`Unavailable`
        when it cannot be told (never ``""``)."""
        from .treejob import escape_path

        raw = self.tree_check(path, env)
        lines = [f"{c['why']}: {escape_path(base64.b64decode(c['path_b64']), c['cut'])}" for c in raw["changed"]]
        if raw["more"]:
            if not lines:
                raise Unavailable("the tree check answered nonsense")  # "more" than nothing is never clean
            lines.append("and more")
        return "\n".join(lines)

    def diff(self, path: str | os.PathLike[str], **kw: Any) -> dict[str, Any]:
        """:func:`orch.gitread.ops.diff` for the repository ``path`` is in (keywords as :func:`diff`)."""
        _top, pins = self._open(path)
        with Repo(pins) as repo:
            return ops.diff(
                repo,
                kw["branch"],
                kw.get("default_branch"),
                kw.get("context", 3),
                kw.get("max_bytes", 262144),
                kw.get("ext"),
            )


#: the one in-process reader of direct mode (#565)
READER = LocalGitReader()


def observe(path: str | os.PathLike[str], refs: list[str], roots: list[str] | None = None) -> dict[str, Any]:
    real, rs = _roots(path, roots)
    with Repo(pin_repo(real, rs)) as repo:
        return ops.observe(repo, refs)


def diff(
    path: str | os.PathLike[str],
    *,
    branch: str,
    default_branch: str | None,
    context: int = 3,
    max_bytes: int = 262144,
    roots: list[str] | None = None,
    ext: dict[str, Any] | None = None,
) -> dict[str, Any]:
    real, rs = _roots(path, roots)
    with Repo(pin_repo(real, rs)) as repo:
        return ops.diff(repo, branch, default_branch, context, max_bytes, ext)


def tree_check(path: str | os.PathLike[str], roots: list[str] | None = None) -> dict[str, Any]:
    """``TreeCheckResult`` in-process, exactly as the host's child answers it (tests and tools)."""
    from . import treecheck, treejob
    from .fs import open_pin

    real, rs = _roots(path, roots)
    pins = pin_repo(real, rs)
    with Repo(pins) as repo:
        worktree = open_pin(pins.worktree)
        try:
            return treejob.result_of(treecheck.check(repo, worktree))
        finally:
            worktree.close()
