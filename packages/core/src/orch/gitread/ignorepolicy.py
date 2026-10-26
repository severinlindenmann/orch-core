"""THE one place that decides whether, and which, ignore rules the reader applies (#583 round 3, structural), and
the one place that parses and applies them.

**The invariant.** *Removal never depends on ignore rules; a status check applies a rule only as git would, and where
it cannot tell which way git matches it applies the rule only if git would in either case.* The answer can only be
dirtier than git's, never cleaner.

* A **removal** check applies no ignore rule at all (:meth:`IgnorePolicy.removal`): every file that is not
  tracked-and-unchanged blocks it, ignored or not.
* A **status** check (D58, the runner, the verify preflight, ``list --check``, the host's ``tree.check``) applies the
  rules of git's sources, in git's order of precedence (lowest first): the person's ``core.excludesFile``,
  ``info/exclude``, the ``.gitignore`` files from the top down.

**Case (``core.ignorecase``; owner decision B, 19 Oct 2026).** git matches ignore patterns case-insensitively
(``WM_CASEFOLD``) exactly when ``core.ignorecase`` is true, which ``git init`` and ``git clone`` set on a
case-insensitive file system such as macOS's. The reader matches the same way (:mod:`orch.gitread.wildmatch`, git's
quirks included) and takes the value as git does: the repository's config (the last value) over the person's, else
git's default, false. The **modes** (``folds``) a check runs:

* the value is known: that one mode;
* it is not known (the repository's config has an ``include``/``includeIf`` or a value that is not a boolean; the
  person's configuration cannot be read, as in host mode, or has an include, or the environment passes configuration):
  **both** modes, and a path is ignored only if it is ignored in both. Whichever mode git is in, the reader has then
  ignored no more than git.

The person's ``core.excludesFile`` (the lowest source) is applied only when the person's configuration is known, no
repository config sets ``core.excludesFile`` and the file is certain; otherwise it is dropped, which is only ever
dirtier. In host mode (no person configuration) the repository's own ``info/exclude`` and ``.gitignore`` files apply.

**What is not certain** is dropped *together with every source of lower precedence below it* (dropping only the
source could drop a negation, and the lower sources would then ignore what git shows). A source is not certain when:

* it is not a regular file that could be read (a FIFO, an unreadable file; a symlinked ``.gitignore`` is skipped, as
  git skips it);
* a negation line holds a byte outside printable ASCII (macOS git precomposes file names to NFC, the reader reads
  them as they are: a non-matching negation would hide a file git shows), or any line holds a control byte other than
  tab and CR, bytes that are not UTF-8, or text that is not NFC;
* it is the person's ``core.excludesFile`` while the repository's own config sets ``core.excludesFile``.

Parsing is git's ``add_patterns_from_buffer``: a leading UTF-8 BOM is skipped, a line is split at ``\\n``, one trailing
CR is dropped, ``#`` starts a comment, trailing spaces are trimmed by git's ``trim_trailing_spaces`` (a backslash
protects the next byte), ``!`` negates, a trailing ``/`` means directories only.

All ignore files of one check together are capped at :data:`BYTES_MAX` (more: unknown, never a guess), and parsing
spends the check's :class:`~orch.gitread.wildmatch.Budget` in proportion to the bytes.

Nothing else in the reader parses or applies an ignore pattern: ``tests/gitread/test_ignore_policy.py`` checks that
:func:`_parse` and :func:`~orch.gitread.wildmatch.compile_glob` are used only here.
"""

from __future__ import annotations

import os
import stat
import unicodedata
from typing import Any

from .errors import Unavailable
from .fs import Dir
from .wildmatch import Budget, Glob, compile_glob

__all__ = ["BYTES_MAX", "IgnorePolicy", "Ignores", "PersonIgnore", "git_bool"]

#: all ignore files of one check together (#583 round 3, C): more is unknown
BYTES_MAX = 256 * 1024
_BOM = b"\xef\xbb\xbf"


def git_bool(value: str | None) -> bool | None:
    """git's ``git_config_bool`` for a value from :func:`orch.gitread.config.parse`: ``None`` (a key with no ``=``) is
    true, ``""`` false, ``true|yes|on`` / ``false|no|off`` in any case, otherwise an integer (non-zero is true).
    ``None`` is returned for anything else (git dies on it: not certain)."""
    if value is None:
        return True
    low = value.lower()
    if low == "":
        return False
    if low in ("true", "yes", "on"):
        return True
    if low in ("false", "no", "off"):
        return False
    if low.isascii() and low.lstrip("+-").isdigit():
        return int(low) != 0
    return None


class Pattern:
    """One ignore line, compiled once per mode in ``folds`` (``globs[i]`` for ``folds[i]``)."""

    __slots__ = ("base", "lbase", "globs", "negate", "dir_only", "anchored")

    def __init__(
        self, base: bytes, globs: tuple[Glob | None, ...], negate: bool, dir_only: bool, anchored: bool
    ) -> None:
        self.base = base
        self.lbase = base.lower()  # bytes.lower() is ASCII-only, as git's (fspathncmp under core.ignorecase)
        self.globs = globs
        self.negate = negate
        self.dir_only = dir_only
        self.anchored = anchored

    def match(self, path: bytes, is_dir: bool, mode: int, fold: bool, budget: Budget | None = None) -> bool:
        glob = self.globs[mode]
        if glob is None or (self.dir_only and not is_dir):
            return False
        if self.base:
            head = path[: len(self.base)]
            if (head.lower() != self.lbase) if fold else (head != self.base):  # git: fspathncmp
                return False
            if path[len(self.base) : len(self.base) + 1] != b"/":
                return False
            path = path[len(self.base) + 1 :]
        if not self.anchored:
            path = path.rsplit(b"/", 1)[-1]
        return glob.match(path, budget)


class Ignores:
    """The stack of pattern lists in force at one directory; a new, empty one below a source that was dropped."""

    def __init__(self, budget: Budget | None, folds: tuple[bool, ...]) -> None:
        self.levels: list[list[Pattern]] = []
        self.budget = budget
        self.folds = folds

    def ignored(self, path: bytes, is_dir: bool) -> bool:
        """The last matching pattern decides; deeper files win over shallower ones, the person's file is lowest.
        With more than one mode (git's ``core.ignorecase`` not known): ignored only if every mode ignores it.

        **Never ignored: a path with a non-ASCII component that is not NFC** (or not UTF-8). git on macOS
        (``core.precomposeunicode``, its ``git init`` default) matches the NFC form of an on-disk NFD name, this reader
        matches the bytes it lists, so a pattern could match in one and not the other (Opus delta L1: ``cafe*`` against
        an NFD ``café``). Such a path always counts as untracked: dirtier, never cleaner. Because a directory is judged
        here too, nothing below it is pruned either."""
        if not path.isascii() and not _nfc(path):
            return False
        for mode, fold in enumerate(self.folds):
            if not self._ignored_in(path, is_dir, mode, fold):
                return False
        return True

    def _ignored_in(self, path: bytes, is_dir: bool, mode: int, fold: bool) -> bool:
        budget = self.budget
        for pats in reversed(self.levels):
            for p in reversed(pats):
                if budget is not None:
                    budget.check()  # the clock, per pattern (#583 round 3, C)
                if p.match(path, is_dir, mode, fold, budget):
                    return not p.negate
        return False


def _nfc(path: bytes) -> bool:
    """Whether a path with non-ASCII bytes is valid UTF-8 in NFC (a whole path is NFC iff every component is)."""
    try:
        text = path.decode("utf-8")
    except UnicodeDecodeError:
        return False
    return unicodedata.normalize("NFC", text) == text


def _trim_trailing_spaces(line: bytes) -> bytes:
    """git's ``trim_trailing_spaces``: a backslash protects the byte after it; the last run of spaces is cut."""
    last = -1
    i, n = 0, len(line)
    while i < n:
        c = line[i]
        if c == 0x20:
            if last < 0:
                last = i
        elif c == 0x5C:
            i += 1
            if i >= n:
                return line  # a lone trailing backslash: nothing is trimmed
            last = -1
        else:
            last = -1
        i += 1
    return line if last < 0 else line[:last]


def _line_sure(line: bytes, negate: bool) -> bool:
    """Whether this reader is known to read ``line`` (after trimming and the ``!``) as git does."""
    ascii_only = True
    for c in line:
        if c >= 0x80:
            ascii_only = False
        elif (c < 0x20 and c not in (0x09, 0x0D)) or c == 0x7F:
            return False
    if ascii_only:
        return True
    if negate:
        return False  # a negation that does not match leaves the file ignored: cleaner than git
    try:
        text = line.decode("utf-8")
    except UnicodeDecodeError:
        return False
    return unicodedata.normalize("NFC", text) == text


def _parse(data: bytes, base: bytes, budget: Budget | None, folds: tuple[bool, ...]) -> list[Pattern] | None:
    """The patterns of one ignore file whose directory is ``base`` (``b""``: the top), or ``None`` when some line is
    not certain (the caller drops the file and every lower source)."""
    if data.startswith(_BOM):
        data = data[len(_BOM) :]  # git: skip_utf8_bom
    out: list[Pattern] = []
    for raw in data.split(b"\n"):
        if budget is not None:
            budget.spend(len(raw) + 1)
        if not raw or raw[0] == 0x23:  # empty, or a comment
            continue
        line = raw[:-1] if raw.endswith(b"\r") else raw
        line = _trim_trailing_spaces(line)
        negate = line.startswith(b"!")
        if negate:
            line = line[1:]
        if not _line_sure(line, negate):
            return None
        dir_only = line.endswith(b"/")
        if dir_only:
            line = line[:-1]
        if not line:
            continue  # an empty pattern matches nothing
        anchored = b"/" in line
        if line.startswith(b"/"):
            line = line[1:]
        # git's wildmatch, no backtracking; None: git matches nothing with this line
        globs = tuple(compile_glob(line, budget, fold) for fold in folds)
        if any(g is not None for g in globs):
            out.append(Pattern(base, globs, negate, dir_only, anchored))
    return out


class PersonIgnore:
    """What direct mode knows of the person's configuration (:func:`orch.gitread.local.person_ignore`): ``known`` (every
    file git reads could be read with certainty), ``ignorecase`` (the last ``core.ignorecase`` of those files, ``None``
    when none sets it), ``excludes`` (the bytes of the file git uses as ``core.excludesFile``, ``None``: none or not
    certain)."""

    __slots__ = ("known", "ignorecase", "excludes", "why", "env_config")

    def __init__(
        self,
        known: bool,
        ignorecase: bool | None = None,
        excludes: bytes | None = None,
        why: str = "",
        env_config: bool = False,
    ) -> None:
        self.known, self.ignorecase, self.excludes, self.why = known, ignorecase, excludes, why
        #: the environment passes configuration (``GIT_CONFIG_COUNT``/``GIT_CONFIG_PARAMETERS``): git ranks it above
        #: the repository's config, so the repository's ``core.ignorecase`` is not final (#724)
        self.env_config = env_config


class IgnorePolicy:
    """See the module docstring. Build one with :meth:`removal` or :meth:`status`."""

    def __init__(
        self, why: str | None, budget: Budget | None, person: bytes | None = None, folds: tuple[bool, ...] = (False,)
    ) -> None:
        #: ``None``: rules apply (source by source); else why none applies
        self.why = why
        self.budget = budget
        self._person = person
        #: the case modes a rule is matched in (see the module docstring): ignored only if ignored in every one
        self.folds = folds
        self._bytes = 0

    @classmethod
    def removal(cls, budget: Budget | None = None) -> IgnorePolicy:
        return cls("a removal never depends on ignore rules", budget)

    @classmethod
    def status(cls, repo: Any, person: PersonIgnore | None, budget: Budget | None) -> IgnorePolicy:
        """The policy of one status check of ``repo``. ``person``: ``None`` when the person's configuration cannot be
        read here (host mode's reader child)."""
        from .config import parse

        try:
            entries = parse(repo.commondir.read("config", 1 << 20))
        except Unavailable:
            return cls("the repository's config cannot be read with certainty", budget)
        unsure = False  # an include or a value git would reject: the repository's own ignorecase is not known
        repo_ic: bool | None = None
        excludes = person.excludes if person is not None and person.known else None
        for section, sub, key, value in entries:
            if section in ("include", "includeif"):
                unsure = True
            elif section == "core" and sub is None and key == "ignorecase":
                got = git_bool(value)
                if got is None:
                    unsure = True
                else:
                    repo_ic = got
            elif section == "core" and sub is None and key == "excludesfile":
                excludes = None  # git's file is then the repository's: not this reader's to guess (lowest: drop)
        if unsure or (person is not None and person.env_config):
            folds: tuple[bool, ...] = (False, True)
        elif repo_ic is not None:
            folds = (repo_ic,)
        elif person is not None and person.known:
            folds = (bool(person.ignorecase),)  # unset everywhere: git's default, false
        else:
            folds = (False, True)
        if unsure or person is None or not person.known:
            excludes = None  # an include may set core.excludesFile; no person configuration: not known
        return cls(None, budget, excludes, folds)

    # -- the sources, each read and judged here

    def _empty(self) -> Ignores:
        return Ignores(self.budget, self.folds)

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
            chunks: list[bytes] = []
            got = 0
            while got <= left:  # to EOF (a read may be short), but never past the cap
                chunk = os.read(fd, min(65536, left + 1 - got))
                if not chunk:
                    break
                chunks.append(chunk)
                got += len(chunk)
            data = b"".join(chunks)
        finally:
            os.close(fd)
        self._bytes += len(data)
        if self._bytes > BYTES_MAX:
            raise Unavailable(f"the ignore files of this working tree are larger than {BYTES_MAX >> 10} KiB")
        if self.budget is not None:
            self.budget.check()
        return data, True

    def top(self, commondir: Dir) -> Ignores:
        """The stack at the top of the working copy: the person's file, then ``info/exclude``, as far as certain."""
        ign = self._empty()
        if self.why is not None:
            return ign
        person = self._person
        if person is not None:
            self._bytes += len(person)
            if self._bytes > BYTES_MAX:
                raise Unavailable(f"the ignore files of this working tree are larger than {BYTES_MAX >> 10} KiB")
            got = _parse(person, b"", self.budget, self.folds)
            if got is not None:  # the lowest source: dropping it alone is only ever dirtier
                ign.levels.append(got)
        data, sure = self._take(commondir, "info/exclude")
        if not sure:
            return self._empty()  # dropped, and the person's file below it with it
        if data is not None:
            got = _parse(data, b"", self.budget, self.folds)
            if got is None:
                return self._empty()
            ign.levels.append(got)
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
            return self._empty(), False
        if data is None:
            return ign, False
        got = _parse(data, rel, self.budget, self.folds)
        if got is None:
            return self._empty(), False
        ign.levels.append(got)
        return ign, True
