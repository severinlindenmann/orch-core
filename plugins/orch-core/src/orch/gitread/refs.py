"""``HEAD``, loose and packed refs, resolved as git resolves them (F7).

* A loose ref wins over ``packed-refs`` (git's order: a loose file that disagrees with ``packed-refs`` is the ref).
* ``HEAD`` must be exactly ``ref: refs/heads/<branch>\\n``.
* The repository is refused (:class:`~orch.gitread.errors.Unavailable`) when a loose ref is itself a symref (or a
  symlink; the one exception, skipped and never followed, is git clone's ``refs/remotes/<remote>/HEAD``), when two
  ref names differ only in ASCII case (in either store or across them: on a case-insensitive file system they are one
  file, so which one git reads depends on the machine), when a loose ref or a
  ``packed-refs`` line cannot be parsed, or when ``packed-refs`` names one ref twice.
* Loose files whose names git would not accept as refs (``*.lock``, a leading dot, ...) are skipped, as git skips
  them. ``refs/replace/`` is never used for lookups.
"""

from __future__ import annotations

import re

from . import limits
from .errors import Unavailable
from .fs import Dir

__all__ = ["RefStore", "check_refname", "read_head", "read_head_any"]

_BAD_CHARS = re.compile(r"[\x00-\x20\x7f~^:?*\[\\]")


def check_refname(name: str) -> bool:
    """git's ``check_refname_format`` for a full name (at least two components), without ``--allow-onelevel``."""
    if not name or not name.isascii() or name.endswith("/") or name.endswith(".") or name == "@":
        return False
    if ".." in name or "@{" in name or _BAD_CHARS.search(name):
        return False
    parts = name.split("/")
    if len(parts) < 2:
        return False
    return all(p and not p.startswith(".") and not p.endswith(".lock") for p in parts)


def read_head(gitdir: Dir) -> str:
    """The branch ref ``HEAD`` names (``refs/heads/<branch>``)."""
    data = gitdir.read("HEAD", limits.SMALL_FILE_MAX)
    m = re.fullmatch(rb"ref: (refs/heads/[^\n]+)\n", data)
    if not m:
        raise Unavailable("HEAD is not 'ref: refs/heads/<branch>'")
    name = m.group(1).decode("ascii", errors="replace")
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
    target = m.group(1).decode("ascii", errors="replace")
    return target.startswith(f"refs/remotes/{parts[2]}/") and target != full and check_refname(target)


class RefStore:
    """Every ref of the repository, read once (loose files under ``refs/`` and ``packed-refs`` in the ``commondir``)."""

    def __init__(self, commondir: Dir, hexlen: int) -> None:
        self.hexlen = hexlen
        self.loose: dict[str, str] = {}
        self.packed: dict[str, str] = {}
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
            low = name.lower()
            other = seen.setdefault(low, name)
            if other != name:
                raise Unavailable(f"the refs {other!r} and {name!r} differ only in case")

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
        for i, line in enumerate(lines):
            if i == 0 and line.startswith(b"# pack-refs with:"):
                continue
            if line.startswith(b"^"):
                if last is None or not self._hex.fullmatch(line[1:]):
                    raise Unavailable(f"packed-refs line {i + 1} cannot be parsed")
                last = None  # one peeled line per ref
                continue
            hexid, sp, name_b = line.partition(b" ")
            name = name_b.decode("ascii", errors="replace")
            if not sp or not self._hex.fullmatch(hexid) or not check_refname(name):
                raise Unavailable(f"packed-refs line {i + 1} cannot be parsed")
            if name in self.packed:
                raise Unavailable(f"packed-refs names {name!r} twice")
            self.packed[name] = hexid.decode("ascii")
            last = name

    def resolve(self, name: str) -> str | None:
        """The object id ``name`` points at, or ``None`` when there is no such ref."""
        if not check_refname(name):
            raise Unavailable(f"invalid ref name {name!r}")
        if name in self.loose:
            return self.loose[name]
        return self.packed.get(name)
