"""Tree-to-tree diff and its patch text, as ``git diff --no-ext-diff --no-textconv --no-renames <base> <head>``
prints it with no repository or user config (core.quotePath on, a/ and b/ prefixes, abbreviated ids as git
abbreviates them, binary files by git's NUL test, a type change split into a deletion and a creation).

Not read, by design: ``.gitattributes`` (so no ``diff``/``binary``/``funcname`` attributes), external diff drivers,
textconv. Renames are not detected (see the PR notes: section 0.8 asks for 50 % rename detection, a follow-up).
"""

from __future__ import annotations

from dataclasses import dataclass

from . import limits
from .errors import Unavailable
from .repo import Repo, TreeEntry
from .xdiff import unified

__all__ = ["Change", "diff_trees", "quote_c", "render"]

_TREE = 0o040000
_GITLINK = 0o160000
_LINK = 0o120000
_IFMT = 0o170000
_FIRST_FEW_BYTES = 8000
_C_ESCAPES = {7: b"\\a", 8: b"\\b", 9: b"\\t", 10: b"\\n", 11: b"\\v", 12: b"\\f", 13: b"\\r", 34: b'\\"', 92: b"\\\\"}


@dataclass
class Change:
    path: bytes
    old_mode: int  # 0: absent
    old_oid: str | None
    new_mode: int
    new_oid: str | None


def _key(e: TreeEntry) -> bytes:
    return e.name + b"/" if e.is_tree else e.name


def diff_trees(repo: Repo, old: str | None, new: str | None) -> list[Change]:
    """Every changed path below two trees, in git's traversal order."""
    out: list[Change] = []
    budget = [limits.TREE_ENTRIES_MAX]

    def entries(oid: str | None) -> list[TreeEntry]:
        if oid is None:
            return []
        es = repo.tree(oid)
        budget[0] -= len(es)
        if budget[0] < 0:
            raise Unavailable("too many paths to compare")
        return sorted(es, key=_key)

    def walk(old_t: str | None, new_t: str | None, prefix: bytes) -> None:
        a, b = entries(old_t), entries(new_t)
        i = j = 0
        while i < len(a) or j < len(b):
            ka = _key(a[i]) if i < len(a) else None
            kb = _key(b[j]) if j < len(b) else None
            if kb is None or (ka is not None and ka < kb):
                gone(a[i], prefix)
                i += 1
            elif ka is None or kb < ka:
                came(b[j], prefix)
                j += 1
            else:
                ea, eb = a[i], b[j]
                i += 1
                j += 1
                if ea.is_tree:  # both trees (same key)
                    if ea.oid != eb.oid:
                        walk(ea.oid, eb.oid, prefix + ea.name + b"/")
                elif ea.oid != eb.oid or ea.mode != eb.mode:
                    out.append(Change(prefix + ea.name, ea.mode, ea.oid, eb.mode, eb.oid))

    def gone(e: TreeEntry, prefix: bytes) -> None:
        if e.is_tree:
            walk(e.oid, None, prefix + e.name + b"/")
        else:
            out.append(Change(prefix + e.name, e.mode, e.oid, 0, None))

    def came(e: TreeEntry, prefix: bytes) -> None:
        if e.is_tree:
            walk(None, e.oid, prefix + e.name + b"/")
        else:
            out.append(Change(prefix + e.name, 0, None, e.mode, e.oid))

    walk(old, new, b"")
    return out


def quote_c(name: bytes) -> tuple[bytes, bool]:
    """git's ``quote_c_style`` body (core.quotePath on): the escaped bytes and whether quoting was needed."""
    out = bytearray()
    need = False
    for c in name:
        esc = _C_ESCAPES.get(c)
        if esc is not None:
            out += esc
            need = True
        elif c < 0x20 or c >= 0x7F:
            out += b"\\%03o" % c
            need = True
        else:
            out.append(c)
    return bytes(out), need


def _quote_two(prefix: bytes, name: bytes) -> bytes:
    q1, n1 = quote_c(prefix)
    q2, n2 = quote_c(name)
    if n1 or n2:
        return b'"' + q1 + q2 + b'"'
    return prefix + name


def _is_binary(data: bytes) -> bool:
    return b"\0" in data[:_FIRST_FEW_BYTES]


def _content(repo: Repo, mode: int, oid: str | None) -> bytes:
    if oid is None:
        return b""
    if mode == _GITLINK:
        return b"Subproject commit %s\n" % oid.encode()
    return repo.blob(oid)


def _one(repo: Repo, path: bytes, mode1: int, oid1: str | None, mode2: int, oid2: str | None, context: int):
    """``builtin_diff`` for one pair (either side may be absent): ``(text, added, deleted, binary)``."""
    a_one = _quote_two(b"a/", path)
    b_two = _quote_two(b"b/", path)
    lbl0 = a_one if oid1 is not None else b"/dev/null"
    lbl1 = b_two if oid2 is not None else b"/dev/null"
    header = b"diff --git " + a_one + b" " + b_two + b"\n"
    index = b""
    if oid1 != oid2:
        z = "0" * repo.hexlen
        index = b"index %s..%s" % (
            repo.objects.abbrev(oid1 or z).encode(),
            repo.objects.abbrev(oid2 or z).encode(),
        )
        if mode1 == mode2:
            index += b" %06o" % mode1
        index += b"\n"
    must_show = False
    if oid1 is None:
        header += b"new file mode %06o\n" % mode2 + index
        must_show = True
    elif oid2 is None:
        header += b"deleted file mode %06o\n" % mode1 + index
        must_show = True
    else:
        if mode1 != mode2:
            header += b"old mode %06o\nnew mode %06o\n" % (mode1, mode2)
            must_show = True
        header += index
    one = _content(repo, mode1, oid1)
    two = _content(repo, mode2, oid2)
    if _is_binary(one) or _is_binary(two):
        if oid1 == oid2 or one == two:
            return (header if must_show else b""), 0, 0, True
        return header + b"Binary files %s and %s differ\n" % (lbl0, lbl1), 0, 0, True
    hunks, added, deleted = unified(one, two, context)
    if not hunks:
        return (header if must_show else b""), 0, 0, False
    minus = b"--- " + lbl0 + (b"\t" if b" " in lbl0 else b"") + b"\n"
    plus = b"+++ " + lbl1 + (b"\t" if b" " in lbl1 else b"") + b"\n"
    return header + minus + plus + b"".join(hunks), added, deleted, False


def render(repo: Repo, changes: list[Change], context: int) -> tuple[bytes, list[dict]]:
    """The patch text and the ``files`` list of section 0.8 (status ``A``, ``M``, ``D`` or ``T``)."""
    chunks: list[bytes] = []
    files: list[dict] = []
    for ch in changes:
        path = ch.path
        if ch.old_oid is not None and ch.new_oid is not None and (ch.old_mode ^ ch.new_mode) & _IFMT:
            t1, a1, d1, b1 = _one(repo, path, ch.old_mode, ch.old_oid, 0, None, context)
            t2, a2, d2, b2 = _one(repo, path, 0, None, ch.new_mode, ch.new_oid, context)
            chunks += (t1, t2)
            status, added, deleted, binary = "T", a1 + a2, d1 + d2, b1 or b2
        else:
            text, added, deleted, binary = _one(repo, path, ch.old_mode, ch.old_oid, ch.new_mode, ch.new_oid, context)
            chunks.append(text)
            status = "A" if ch.old_oid is None else "D" if ch.new_oid is None else "M"
        files.append(
            {
                "path": path.decode("utf-8", errors="replace"),
                "old_path": None,
                "status": status,
                "add": added,
                "del": deleted,
                "binary": binary,
            }
        )
    return b"".join(chunks), files
