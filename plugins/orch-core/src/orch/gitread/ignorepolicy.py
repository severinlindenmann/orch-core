"""THE one place that decides whether, and which, ignore rules the reader applies (#583 round 3, orch-07's
structural decision after the third false clean of the same class).

**The invariant.** *Removal never depends on ignore rules; ignore rules apply only when gitread is sure it matches git,
otherwise none.*

* A **removal** check applies no ignore rule at all (:meth:`IgnorePolicy.removal`): every file that is not
  tracked-and-unchanged blocks it, ignored or not.
* A **status** check (D58, the runner, the verify preflight, ``list --check``, the host's ``tree.check``) applies the
  rules of one source only when every condition under which this reader is known to read it as git does holds. When
  any is not certain, the source is dropped **together with every source of lower precedence below it** (dropping only
  the source could drop a negation, and the lower sources would then ignore what git shows: cleaner). The answer can
  only be dirtier than git, never cleaner.

The sources, in git's order of precedence (lowest first): the person's ``core.excludesFile``, ``info/exclude``, the
``.gitignore`` files from the top down. A whole check applies **none** when:

* the file system of the working copy is case-insensitive (git's ``core.ignorecase`` default there);
* any configuration git reads sets ``core.ignorecase`` true, or cannot be read with certainty (the repository's config
  has an ``include``/``includeIf``, the person's configuration is unknown: always in host mode, whose reader child
  cannot read it);
* :attr:`IgnorePolicy.why` names the reason, for messages.

One source is not certain (and is dropped with everything lower) when it:

* is not a regular file that could be read (a FIFO, an unreadable file; a symlinked ``.gitignore`` is skipped, as git
  skips it);
* starts with a UTF-8 BOM, holds a byte outside printable ASCII other than tab, CR and LF (non-ASCII or non-NFC
  patterns), or a backslash other than a leading ``\\!`` or ``\\#`` (the escapes the parity corpus proves);
* is the person's ``core.excludesFile`` while the repository's own config sets ``core.excludesFile`` (git's value is
  then the repository's).

All ignore files of one check together are capped at :data:`BYTES_MAX` (more: unknown, never a guess), and parsing
spends the check's :class:`~orch.gitread.wildmatch.Budget` in proportion to the bytes.

Nothing else in the reader parses or applies an ignore pattern: ``tests/gitread/test_ignore_policy.py`` checks that
:func:`_parse` and :func:`~orch.gitread.wildmatch.compile_glob` are used only here.
"""

from __future__ import annotations

import os
import stat
from typing import Any

from .errors import Unavailable
from .fs import Dir
from .wildmatch import Budget, Glob, compile_glob

__all__ = ["BYTES_MAX", "IgnorePolicy", "Ignores", "PersonIgnore"]

#: all ignore files of one check together (#583 round 3, C): more is unknown
BYTES_MAX = 256 * 1024
_TRUE = {None, "true", "yes", "on", "1"}


class Pattern:
    __slots__ = ("base", "glob", "negate", "dir_only", "anchored")

    def __init__(self, base: bytes, glob: Glob, negate: bool, dir_only: bool, anchored: bool) -> None:
        self.base = base
        self.glob = glob
        self.negate = negate
        self.dir_only = dir_only
        self.anchored = anchored

    def match(self, path: bytes, is_dir: bool, budget: Budget | None = None) -> bool:
        if self.dir_only and not is_dir:
            return False
        if self.base:
            if not path.startswith(self.base + b"/"):
                return False
            path = path[len(self.base) + 1 :]
        if not self.anchored:
            path = path.rsplit(b"/", 1)[-1]
        return self.glob.match(path, budget)


class Ignores:
    """The stack of pattern lists in force at one directory; a new, empty one below a source that was dropped."""

    def __init__(self, budget: Budget | None = None) -> None:
        self.levels: list[list[Pattern]] = []
        self.budget = budget

    def ignored(self, path: bytes, is_dir: bool) -> bool:
        """The last matching pattern decides; deeper files win over shallower ones, the person's file is lowest."""
        budget = self.budget
        for pats in reversed(self.levels):
            for p in reversed(pats):
                if budget is not None:
                    budget.check()  # the clock, per pattern (#583 round 3, C)
                if p.match(path, is_dir, budget):
                    return not p.negate
        return False


def _sure(data: bytes) -> bool:
    """Whether this reader is known to parse ``data`` (one ignore file) exactly as git does."""
    if data.startswith(b"\xef\xbb\xbf"):
        return False
    for line in data.split(b"\n"):
        for i, c in enumerate(line):
            if c == 0x5C and not (i == 0 and line[1:2] in (b"!", b"#")):
                return False
            if not (0x20 <= c <= 0x7E or c in (0x09, 0x0D)):
                return False
    return True


def _parse(data: bytes, base: bytes, budget: Budget | None) -> list[Pattern]:
    """The patterns of one ignore file whose directory is ``base`` (``b""``: the top). Only for data :func:`_sure`
    accepted."""
    out: list[Pattern] = []
    for line in data.split(b"\n"):
        if budget is not None:
            budget.spend(len(line) + 1)
        if line.endswith(b"\r"):
            line = line[:-1]
        while line.endswith(b" "):  # no backslash survives _sure except a leading one: plain trimming is git's
            line = line[:-1]
        if not line or line.startswith(b"#"):
            continue
        negate = line.startswith(b"!")
        if negate:
            line = line[1:]
        dir_only = line.endswith(b"/")
        if dir_only:
            line = line[:-1]
        if not line:
            continue
        anchored = b"/" in line
        if line.startswith(b"/"):
            line = line[1:]
        glob = compile_glob(line, budget)  # git's wildmatch, no backtracking; None: git matches nothing with it
        if glob is not None:
            out.append(Pattern(base, glob, negate, dir_only, anchored))
    return out


class PersonIgnore:
    """What direct mode knows of the person's configuration (:func:`orch.gitread.local.person_ignore`): ``known`` (every
    file git reads could be read with certainty), ``ignorecase`` (any of them sets ``core.ignorecase``), ``excludes``
    (the bytes of the file git uses as ``core.excludesFile``, ``None``: none or not certain)."""

    __slots__ = ("known", "ignorecase", "excludes", "why")

    def __init__(self, known: bool, ignorecase: bool = False, excludes: bytes | None = None, why: str = "") -> None:
        self.known, self.ignorecase, self.excludes, self.why = known, ignorecase, excludes, why


def _case_insensitive(worktree: Dir) -> bool:
    """Whether the working copy's file system folds case: ``.git`` and ``.GIT`` name the same entry."""
    try:
        a = os.stat(".git", dir_fd=worktree.fd, follow_symlinks=False)
    except OSError:
        return True  # cannot tell: unsure
    try:
        b = os.stat(".GIT", dir_fd=worktree.fd, follow_symlinks=False)
    except FileNotFoundError:
        return False
    except OSError:
        return True
    return (a.st_dev, a.st_ino) == (b.st_dev, b.st_ino)


class IgnorePolicy:
    """See the module docstring. Build one with :meth:`removal` or :meth:`status`."""

    def __init__(self, why: str | None, budget: Budget | None, person: bytes | None = None) -> None:
        #: ``None``: rules apply (source by source); else why none applies
        self.why = why
        self.budget = budget
        self._person = person
        self._bytes = 0

    @classmethod
    def removal(cls, budget: Budget | None = None) -> IgnorePolicy:
        return cls("a removal never depends on ignore rules", budget)

    @classmethod
    def status(cls, repo: Any, worktree: Dir, person: PersonIgnore | None, budget: Budget | None) -> IgnorePolicy:
        """The policy of one status check of ``repo`` whose working copy is ``worktree``. ``person``: ``None`` when
        the person's configuration cannot be read here (host mode's reader child)."""
        from .config import parse

        if person is None or not person.known:
            why = person.why if person is not None else "the person's git config cannot be read here"
            return cls(why or "the person's git config is not certain", budget)
        if person.ignorecase:
            return cls("core.ignorecase is set in the person's git config", budget)
        if _case_insensitive(worktree):
            return cls("the file system is case-insensitive (git's core.ignorecase)", budget)
        try:
            entries = parse(repo.commondir.read("config", 1 << 20))
        except Unavailable:
            return cls("the repository's config cannot be read with certainty", budget)
        excludes = person.excludes
        for section, sub, key, value in entries:
            if section in ("include", "includeif"):
                return cls("the repository's config has an include", budget)
            if section == "core" and sub is None and key == "ignorecase":
                if value is None or value.strip().lower() in _TRUE:
                    return cls("core.ignorecase is set in the repository's config", budget)
                if value.strip().lower() not in ("false", "no", "off", "0"):
                    return cls("core.ignorecase in the repository's config is not a plain boolean", budget)
            if section == "core" and sub is None and key == "excludesfile":
                excludes = None  # git's file is then the repository's: not this reader's to guess (lowest: drop)
        return cls(None, budget, excludes)

    # -- the sources, each read and judged here

    def _take(self, d: Dir, rel: str) -> tuple[bytes | None, bool]:
        """``(data, sure)`` of the ignore file ``rel`` in ``d``: ``(None, True)`` absent (or a symlink, which git
        does not follow either), ``(None, False)`` present but not certain."""
        try:
            kind = d.kind(rel)
        except Unavailable:
            return None, False
        if kind is None or kind == "link":
            return None, True
        if kind != "file":
            return None, False
        left = BYTES_MAX - self._bytes
        try:
            fd, st = d.open_file(rel)
        except Unavailable:
            return None, False
        try:
            if not stat.S_ISREG(st.st_mode):
                return None, False
            if st.st_size > left:
                raise Unavailable(f"the ignore files of this working tree are larger than {BYTES_MAX >> 10} KiB")
            data = os.read(fd, left + 1)
        finally:
            os.close(fd)
        self._bytes += len(data)
        if self._bytes > BYTES_MAX:
            raise Unavailable(f"the ignore files of this working tree are larger than {BYTES_MAX >> 10} KiB")
        if self.budget is not None:
            self.budget.check()
        return data, _sure(data)

    def top(self, commondir: Dir) -> Ignores:
        """The stack at the top of the working copy: the person's file, then ``info/exclude``, as far as certain."""
        ign = Ignores(self.budget)
        if self.why is not None:
            return ign
        person = self._person
        if person is not None:
            self._bytes += len(person)
            if self._bytes > BYTES_MAX:
                raise Unavailable(f"the ignore files of this working tree are larger than {BYTES_MAX >> 10} KiB")
            if _sure(person):
                ign.levels.append(_parse(person, b"", self.budget))
        data, sure = self._take(commondir, "info/exclude")
        if not sure:
            return Ignores(self.budget)  # dropped, and the person's file below it with it
        if data is not None:
            ign.levels.append(_parse(data, b"", self.budget))
        return ign

    def directory(self, d: Dir, rel: bytes, ign: Ignores) -> tuple[Ignores, bool]:
        """The stack in force inside directory ``d`` (``rel`` from the top), from the stack ``ign`` of its parent:
        ``(stack, pushed)``. A ``.gitignore`` that is not certain gives an empty new stack (it and every lower source
        dropped for this subtree)."""
        if self.budget is not None:
            self.budget.check()  # the clock, per directory, whatever the policy
        if self.why is not None:
            return ign, False
        data, sure = self._take(d, ".gitignore")
        if not sure:
            return Ignores(self.budget), False
        if data is None:
            return ign, False
        ign.levels.append(_parse(data, rel, self.budget))
        return ign, True
