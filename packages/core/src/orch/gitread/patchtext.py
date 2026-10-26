"""Git-style patch text for the changes between two trees.

This module is an independent implementation, written from the Myers 1986 paper (see :mod:`orch.gitread.myers`)
and from the output format documented in ``git help diff`` ("Generating patch text with -p"): the ``diff --git``
header, the extended headers (``new file mode``, ``deleted file mode``, ``old mode``/``new mode``, ``index``),
``Binary files ... differ``, and the quoting of unusual path names. It is released under the project's licence
(see LICENSE). It is not derived from any other diff implementation.

``diff_trees`` walks two trees in path order, without renames (a rename is a deletion and an addition, so a status
is ``A``, ``D``, ``M`` or ``T`` and ``old_path`` is ``None``). ``render`` produces the patch bytes and the per-file
summary. A type change (file, symlink, gitlink) is a deletion followed by an addition, as in git.
"""

from __future__ import annotations

from typing import Any

from . import limits
from .errors import Unavailable
from .myers import render as render_hunks
from .myers import split_lines
from .repo import Repo

__all__ = ["Change", "diff_trees", "quote_path", "render"]

_ABBREV = 7
_BINARY_PROBE = 8000
_ESCAPES = {7: b"\\a", 8: b"\\b", 9: b"\\t", 10: b"\\n", 11: b"\\v", 12: b"\\f", 13: b"\\r", 34: b'\\"', 92: b"\\\\"}


def quote_path(name: bytes) -> bytes:
    """A path as git writes it in a header: bytes below space, DEL, ``"``, ``\\`` and bytes of 0x80 or more make it a
    double-quoted C string with octal escapes."""
    if not any(c < 0x20 or c in (0x22, 0x5C, 0x7F) or c >= 0x80 for c in name):
        return name
    out = bytearray(b'"')
    for c in name:
        if c in _ESCAPES:
            out += _ESCAPES[c]
        elif c < 0x20 or c >= 0x7F:
            out += b"\\%03o" % c
        else:
            out.append(c)
    return bytes(out + b'"')


class Change:
    """One changed path: ``old`` and ``new`` are ``(mode, oid)`` or ``None``."""

    __slots__ = ("new", "old", "path", "status")

    def __init__(self, path: bytes, old: tuple[int, str] | None, new: tuple[int, str] | None) -> None:
        self.path, self.old, self.new = path, old, new
        if old is None:
            self.status = "A"
        elif new is None:
            self.status = "D"
        elif _kind(old[0]) != _kind(new[0]):
            self.status = "T"
        else:
            self.status = "M"


def _kind(mode: int) -> int:
    return mode & 0o170000


def diff_trees(repo: Repo, base: str, head: str, files_max: int) -> tuple[list[Change], bool]:
    """The changes from tree ``base`` to tree ``head`` in path order; walking stops at ``files_max`` changes (the
    flag says there were more)."""
    out: list[Change] = []
    budget = [limits.TREE_ENTRIES_MAX]

    def entries(oid: str | None) -> dict[bytes, Any]:
        if oid is None:
            return {}
        got = repo.tree(oid, max_entries=budget[0])
        budget[0] -= len(got)
        return {e.name: e for e in got}

    def walk(prefix: bytes, a_oid: str | None, b_oid: str | None):
        a, b = entries(a_oid), entries(b_oid)
        items: list[tuple[bytes, Any]] = []
        for name in a.keys() | b.keys():
            ea, eb = a.get(name), b.get(name)
            if ea is not None and eb is not None and ea.is_tree == eb.is_tree:
                if ea.is_tree:
                    if ea.oid != eb.oid:
                        items.append((name + b"/", ("tree", name, ea.oid, eb.oid)))
                elif (ea.mode, ea.oid) != (eb.mode, eb.oid):
                    items.append((name, ("leaf", name, ea, eb)))
                continue
            for e, is_old in ((ea, True), (eb, False)):
                if e is None:
                    continue
                if e.is_tree:
                    items.append((name + b"/", ("tree", name, e.oid if is_old else None, None if is_old else e.oid)))
                else:
                    items.append((name, ("leaf", name, e, None) if is_old else ("leaf", name, None, e)))
        items.sort(key=lambda kv: kv[0])
        for _, item in items:
            if item[0] == "tree":
                yield from walk(prefix + item[1] + b"/", item[2], item[3])
            else:
                _, name, ea, eb = item
                yield Change(
                    prefix + name,
                    None if ea is None else (ea.mode, ea.oid),
                    None if eb is None else (eb.mode, eb.oid),
                )

    cut = False
    for ch in walk(b"", base, head):
        if len(out) >= files_max:
            cut = True
            break
        out.append(ch)
    return out, cut


def _content(repo: Repo, side: tuple[int, str] | None) -> bytes:
    if side is None:
        return b""
    if _kind(side[0]) == 0o160000:
        return b"Subproject commit " + side[1].encode() + b"\n"
    return repo.blob(side[1])


def _is_binary(data: bytes) -> bool:
    return b"\0" in data[:_BINARY_PROBE]


def _ab(prefix: bytes, path: bytes) -> bytes:
    return quote_path(prefix + path)


def _label(prefix: bytes, path: bytes) -> bytes:
    q = _ab(prefix, path)
    return q + b"\t" if b" " in path and not q.startswith(b'"') else q


def _abbrev(oid: str | None) -> bytes:
    return oid[:_ABBREV].encode() if oid else b"0" * _ABBREV


def _patch(path: bytes, old: tuple[int, str] | None, new: tuple[int, str] | None, repo: Repo, context: int):
    """One ``diff --git`` block for a path whose old and new sides are the same kind (or one is absent)."""
    a_data, b_data = _content(repo, old), _content(repo, new)
    head = [b"diff --git " + _ab(b"a/", path) + b" " + _ab(b"b/", path) + b"\n"]
    if old is None and new is not None:
        head.append(b"new file mode %06o\n" % new[0])
    elif new is None and old is not None:
        head.append(b"deleted file mode %06o\n" % old[0])
    elif old is not None and new is not None and old[0] != new[0]:
        head.append(b"old mode %06o\nnew mode %06o\n" % (old[0], new[0]))
    same_blob = old is not None and new is not None and old[1] == new[1]
    binary = not same_blob and (_is_binary(a_data) or _is_binary(b_data))
    if same_blob:
        return b"".join(head), 0, 0, False
    index = _abbrev(old[1] if old else None) + b".." + _abbrev(new[1] if new else None)
    if old is not None and new is not None and old[0] == new[0]:  # modified: the mode, when it did not change
        index += b" %06o" % new[0]
    head.append(b"index " + index + b"\n")
    if binary:
        left = b"/dev/null" if old is None else _ab(b"a/", path)
        right = b"/dev/null" if new is None else _ab(b"b/", path)
        head.append(b"Binary files " + left + b" and " + right + b" differ\n")
        return b"".join(head), 0, 0, True
    a_lines, b_lines = split_lines(a_data), split_lines(b_data)
    body, add, dele = render_hunks(a_lines, b_lines, context)
    if body:
        head.append(b"--- " + (b"/dev/null\n" if old is None else _label(b"a/", path) + b"\n"))
        head.append(b"+++ " + (b"/dev/null\n" if new is None else _label(b"b/", path) + b"\n"))
    return b"".join(head) + body, add, dele, False


def _one(repo: Repo, change: Change, context: int, keep: bool = True) -> tuple[bytes, int, int, bool]:
    """``(text, added, deleted, binary)`` of one change; ``text`` is empty when ``keep`` is false."""
    if change.status == "T":
        t1, a1, d1, b1 = _patch(change.path, change.old, None, repo, context)
        t2, a2, d2, b2 = _patch(change.path, None, change.new, repo, context)
        text, add, dele, binary = t1 + t2, a1 + a2, d1 + d2, b1 or b2
    else:
        text, add, dele, binary = _patch(change.path, change.old, change.new, repo, context)
    return (text if keep else b""), add, dele, binary


def render(repo: Repo, changes: list[Change], context: int, max_bytes: int) -> tuple[bytes, list[dict[str, Any]], bool]:
    """``(patch, files, cut)``: the patch text is built only while it is within ``max_bytes`` (``cut`` says it was
    stopped); every file still gets its summary."""
    if context < 0:
        raise Unavailable("bad context")
    parts: list[bytes] = []
    size = 0
    cut = False
    files: list[dict[str, Any]] = []
    for ch in changes:
        keep = size <= max_bytes
        text, add, dele, binary = _one(repo, ch, context, keep)
        if keep:
            parts.append(text)
            size += len(text)
        else:
            cut = True
        files.append(
            {
                "path": ch.path.decode("utf-8", errors="replace"),
                "old_path": None,
                "status": ch.status,
                "add": add,
                "del": dele,
                "binary": binary,
            }
        )
    return b"".join(parts), files, cut
