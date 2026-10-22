"""``HEAD``, loose and packed refs, resolved as git resolves them (F7).

* A loose ref wins over ``packed-refs`` (git's order: a loose file that disagrees with ``packed-refs`` is the ref).
* ``HEAD`` must be exactly ``ref: refs/heads/<branch>\\n``.
* The repository is refused (:class:`~orch.gitread.errors.Unavailable`) when a loose ref is itself a symref (or a
  symlink; the one exception, skipped and never followed, is git clone's ``refs/remotes/<remote>/HEAD``), when two
  ref names differ only in ASCII case or Unicode normalisation (in either store or across them: on a case-insensitive
  or normalising file system they are one file, so which one git reads depends on the machine), when a loose ref or a
  ``packed-refs`` line cannot be parsed, or when ``packed-refs`` names one ref twice.
* Ref names are bytes, read as UTF-8 and checked by git's rules (:func:`check_refname`, #561's validator): a
  non-ASCII branch (``übung``, ``日本語``) is an ordinary branch; a name that is not UTF-8 or that the rules refuse is
  never resolved ("unknown", #583).
* Loose files whose names git would not accept as refs (``*.lock``, a leading dot, ...) are skipped, as git skips
  them. ``refs/replace/`` is never used for lookups.
"""

from __future__ import annotations

import os
import re
import stat
import unicodedata

from . import limits
from .errors import Unavailable
from .fs import Dir

__all__ = ["RefStore", "branch_ok", "check_refname", "read_head", "read_head_any"]

#: never in a ref name (git's ref rules, check-ref-format): ``..``, ``@{``, ``//``, ``: \ ~ ^ ? * [`` and whitespace
_REF_BAD = re.compile(r"\.\.|@\{|//|[:\\~^?*\[]|\s")
#: the longest full ref name the reader accepts (a branch name is at most 255 bytes, :func:`branch_ok`)
REFNAME_MAX = 1024


def branch_ok(name: str, max_bytes: int = 255) -> bool:
    """git's ref rules for a branch name (any non-control Unicode): no leading ``-``, no ``..``, ``@{``, ``//``,
    ``: \\ ~ ^ ? * [``, whitespace or control character, no component starting with ``.`` or ending in ``.lock``,
    no trailing ``/`` or ``.``, not ``@``. Deliberately stricter than git: any Unicode whitespace (not only ASCII)
    is refused, and the name is at most ``max_bytes`` bytes (255 for a branch name).

    #561's revision validator (``orch.gitsafe._branch_ok``), moved here unchanged when #565 left no revision operand in
    :data:`orch.gitsafe.OPS`: it is now the one place these rules live, used by every ref name the reader reads or is
    asked for (:func:`check_refname`). A name that was not valid UTF-8 (decoded with ``surrogateescape``) carries
    surrogates and is refused here (category ``Cs``)."""
    return (
        bool(name)
        and not name.startswith("-")
        and _REF_BAD.search(name) is None
        and not any(unicodedata.category(c) in ("Cc", "Cs") for c in name)
        and not any(part.startswith(".") or part.endswith(".lock") or not part for part in name.split("/"))
        and not name.endswith(("/", "."))
        and name != "@"
        and len(name.encode("utf-8", "surrogatepass")) <= max_bytes
    )


def check_refname(name: str) -> bool:
    """A full ref name (at least two components, no ``--allow-onelevel``) by git's rules, Unicode allowed
    (:func:`branch_ok`, #561's validator; #583: a non-ASCII branch is a branch, not "unknown"); its branch part
    (after ``refs/heads/``) at most 255 bytes, the whole name at most :data:`REFNAME_MAX`."""
    if len(name.split("/")) < 2 or not branch_ok(name, REFNAME_MAX):
        return False
    return not name.startswith("refs/heads/") or branch_ok(name[len("refs/heads/") :])


def _fold(name: str) -> str:
    """The name a case-insensitive, normalising file system (APFS, HFS+, NTFS) stores ``name`` as: NFC, then
    case-folded."""
    return unicodedata.normalize("NFC", name).casefold()


def _utf8(raw: bytes) -> str:
    """A ref name's bytes as text: UTF-8, with ``surrogateescape`` for bytes that are not (which
    :func:`check_refname` then refuses: the name is unknown, never a crash and never another name)."""
    return raw.decode("utf-8", errors="surrogateescape")


def read_head(gitdir: Dir) -> str:
    """The branch ref ``HEAD`` names (``refs/heads/<branch>``)."""
    data = gitdir.read("HEAD", limits.SMALL_FILE_MAX)
    m = re.fullmatch(rb"ref: (refs/heads/[^\n]+)\n", data)
    if not m:
        raise Unavailable("HEAD is not 'ref: refs/heads/<branch>'")
    name = _utf8(m.group(1))
    if not check_refname(name):
        raise Unavailable("HEAD names an invalid branch")
    return name


def read_head_any(gitdir: Dir, hexlen: int) -> tuple[str | None, str | None]:
    """Direct mode (#565): ``HEAD`` as ``(branch ref, None)`` for ``ref: refs/heads/<branch>\\n`` or ``(None, id)``
    for a detached ``HEAD`` (a full object id of this repository's format and a newline, as git writes it). Anything
    else is refused, as :func:`read_head` refuses it; host mode keeps :func:`read_head` (a branch only)."""
    data = gitdir.read("HEAD", limits.SMALL_FILE_MAX)
    if re.fullmatch(rb"[0-9a-f]{%d}\n" % hexlen, data):
        return None, data[:-1].decode("ascii")
    return read_head(gitdir), None


def _remote_head(full: str, data: bytes) -> bool:
    """The one symbolic loose ref git itself writes in an ordinary repository: ``refs/remotes/<remote>/HEAD`` naming a
    branch of the same remote (``git clone``, ``git remote set-head``). It is skipped, never followed: orch resolves
    only ``HEAD`` and ``refs/heads/*``, so it changes no answer, and refusing it made every clone unreadable (#565).
    Any other symbolic loose ref still refuses the repository (F7)."""
    parts = full.split("/")
    if len(parts) != 4 or parts[:2] != ["refs", "remotes"] or parts[3] != "HEAD":
        return False
    m = re.fullmatch(rb"ref: (refs/remotes/[^\n]+)\n", data)
    if not m:
        return False
    target = _utf8(m.group(1))
    return target.startswith(f"refs/remotes/{parts[2]}/") and target != full and check_refname(target)


class RefStore:
    """Every ref of the repository, read once (loose files under ``refs/`` and ``packed-refs`` in the ``commondir``)."""

    def __init__(self, commondir: Dir, hexlen: int) -> None:
        self.hexlen = hexlen
        self._commondir = commondir  # open for the Repo's lifetime: the file-system probe of resolve()
        self.loose: dict[str, str] = {}
        self.packed: dict[str, str] = {}
        #: skipped loose symrefs (git clone's refs/remotes/<r>/HEAD): never followed, never answered (#583, L3)
        self.symrefs: set[str] = set()
        self._hex = re.compile(rb"[0-9a-f]{%d}" % hexlen)
        if commondir.kind("refs") == "dir":
            with commondir.sub("refs") as refs:
                self._walk(refs, "refs")
        elif commondir.kind("refs") is not None:
            raise Unavailable("refs is not a directory")
        raw = commondir.read_optional("packed-refs", limits.PACKED_REFS_MAX)
        if raw is not None:
            self._parse_packed(raw)
        seen: dict[str, str] = {}
        for name in (*self.loose, *self.packed):
            other = seen.setdefault(_fold(name), name)
            if other != name:
                raise Unavailable(f"the refs {other!r} and {name!r} differ only in case")
        #: the loose refs by their folded name: on a case-insensitive or normalising file system git's open of a
        #: name that differs only so finds this file (parser differential 2, #583)
        self._folded = {_fold(n): n for n in (*self.loose, *self.symrefs)}

    def _walk(self, d: Dir, prefix: str) -> None:
        for name in sorted(d.listdir()):
            full = f"{prefix}/{name}"
            kind = d.kind(name)
            if kind == "dir":
                with d.sub(name) as sub:
                    self._walk(sub, full)
                continue
            if kind == "link":
                raise Unavailable(f"the ref {full!r} is a symlink")
            if kind != "file":
                raise Unavailable(f"the ref {full!r} is not a regular file")
            if not check_refname(full):
                continue  # git skips these (lock files, names it would refuse)
            if len(self.loose) >= limits.LOOSE_REFS_MAX:
                raise Unavailable("too many loose refs")
            data = d.read(name, limits.SMALL_FILE_MAX)
            if data.startswith(b"ref:"):
                if _remote_head(full, data):
                    self.symrefs.add(full)
                    continue  # git clone's refs/remotes/<r>/HEAD: never followed, never a ref here (#565)
                raise Unavailable(f"the loose ref {full!r} is a symbolic ref")
            if len(data) != self.hexlen + 1 or data[-1:] != b"\n" or not self._hex.fullmatch(data[:-1]):
                raise Unavailable(f"the loose ref {full!r} cannot be parsed")
            self.loose[full] = data[:-1].decode("ascii")

    def _parse_packed(self, raw: bytes) -> None:
        if not raw:
            return
        if not raw.endswith(b"\n"):
            raise Unavailable("packed-refs does not end with a newline")
        lines = raw[:-1].split(b"\n")
        last: str | None = None
        traits: set[bytes] = set()
        order: list[bytes] = []
        for i, line in enumerate(lines):
            if i == 0 and line.startswith(b"# pack-refs with:"):
                traits = set(line[len(b"# pack-refs with:") :].split())
                continue
            if line.startswith(b"^"):
                if last is None or not self._hex.fullmatch(line[1:]):
                    raise Unavailable(f"packed-refs line {i + 1} cannot be parsed")
                last = None  # one peeled line per ref
                continue
            hexid, sp, name_b = line.partition(b" ")
            name = _utf8(name_b)
            if not sp or not self._hex.fullmatch(hexid) or not check_refname(name):
                raise Unavailable(f"packed-refs line {i + 1} cannot be parsed")
            if name in self.packed:
                raise Unavailable(f"packed-refs names {name!r} twice")
            self.packed[name] = hexid.decode("ascii")
            order.append(name_b)
            last = name
        # Parser differential 1 (#583): a file whose header says ``sorted`` is binary-searched by git, so an entry out
        # of order can be missing for git and present here. git writes sorted files; one that claims it and is not
        # was written by someone else: unknown, never a different answer.
        if b"sorted" in traits and any(a >= b for a, b in zip(order, order[1:], strict=False)):
            raise Unavailable("packed-refs says it is sorted and is not")

    def resolve(self, name: str) -> str | None:
        """The object id ``name`` points at, or ``None`` when there is no such ref."""
        if not check_refname(name):
            raise Unavailable(f"invalid ref name {name!r}")
        if name in self.symrefs:  # git would follow it (and it wins over packed-refs): unknown here, never a guess
            raise Unavailable(f"the ref {name!r} is a symbolic ref, which the reader never follows")
        if name in self.loose:
            return self.loose[name]
        alias = self._folded.get(_fold(name))
        if alias is not None:  # git may open that file under this name (differential 2): unknown, never "no such ref"
            raise Unavailable(f"the ref {name!r} differs from the loose ref {alias!r} only in case or normalisation")
        if self._fs_finds(name):
            # #583 round 3 (Opus delta L2): the listing named no such file, yet the file system opens one under this
            # name (APFS folds case and Unicode forms that no fold here reproduces exactly). git opens that file, so
            # only a byte-exact match against the listing is an answer; anything the file system adds is unknown.
            raise Unavailable(f"the ref {name!r} is not byte-exact against the refs directory")
        return self.packed.get(name)

    def _fs_finds(self, name: str) -> bool:
        """Whether the file system finds an entry at ``name`` below the commondir (an ``lstat``, nothing opened)."""
        try:
            st = os.stat(name, dir_fd=self._commondir.fd, follow_symlinks=False)
        except (FileNotFoundError, NotADirectoryError):
            return False
        except OSError:
            return True  # cannot tell: treat as found (unknown)
        except ValueError:
            return True  # a name the OS cannot even take (an embedded NUL): unknown
        return not stat.S_ISDIR(st.st_mode)  # a directory is no ref to git either: it reads packed-refs then
