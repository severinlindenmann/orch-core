"""Populating a fresh linked worktree without git's checkout (#584, the residual of #565/#583).

``git worktree add`` checks files out, and a checkout runs the smudge/process filters that the repository's **own**
config names, read when git starts: an agent that commits a repo config (``.git/config`` is shared by every linked
worktree) followed by a column-G ``worktree.create`` could make a program run as the person. So the runner now runs
``git worktree add --no-checkout`` (no checkout, no filter, no hook) and :func:`populate` writes the tree itself:

* the commit ``HEAD`` names is read with the reader (:class:`orch.gitread.repo.Repo`, objects verified by id); files
  are written relative to a directory fd with ``O_CREAT|O_EXCL|O_NOFOLLOW`` (modes 100644 and 100755 from the
  umask as git does, symlinks, gitlinks as empty directories); then the index (version 2, stat data from what was
  written) is written under ``index.lock`` and renamed;
* **filters: none.** No external process runs during the checkout, ever. A path whose ``filter`` attribute names any
  filter (git-lfs included; the attribute comes from the tree's ``.gitattributes``, ``info/attributes`` or the person's
  global attributes) refuses the whole worktree: "this repo uses Git LFS / filters; worktrees for it are not supported
  yet". The repo's config is never read for a filter either. (Third time the class "repo-controlled config executes
  code during worktree add" was found: #565, #584, git-lfs ``lfs.customtransfer.*`` through the person's smudge.)
* **fail closed** (:class:`~orch.gitread.errors.Unavailable`, the caller removes the half-populated worktree): a name
  git's ``verify_path`` would refuse (``.git`` in any spelling, also as ``git~1`` or with ignorable code points), a
  ``.gitmodules`` symlink, an object the reader cannot read (including one over :data:`limits.OBJECT_MAX`), a name that
  collides on this file system, a path git would convert (``ident``, ``working-tree-encoding``, ``eol=crlf``,
  the person's ``core.autocrlf=true`` or ``core.eol=crlf``), a ``.gitattributes`` it cannot parse with certainty.
"""

from __future__ import annotations

import contextlib
import hashlib
import os
import re
import stat
import struct
import time
import unicodedata
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field

from . import limits
from .config import parse
from .errors import Unavailable
from .fs import Dir, open_pin
from .local import SYSTEM_CONFIGS, _read_small
from .pinning import pin_repo
from .refs import read_head_any
from .repo import Repo
from .wildmatch import Glob, compile_glob

__all__ = ["populate"]

ATTRS_MAX = 1000
POPULATE_SECONDS = 600.0
ATTR_FILE_MAX = limits.MiB
DEPTH_MAX = 200
_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_CLOEXEC = getattr(os, "O_CLOEXEC", 0)
_DIRECTORY = getattr(os, "O_DIRECTORY", 0)
_WANT = ("filter", "ident", "working-tree-encoding", "eol")
_IGNORABLE = re.compile("[​-‏‪-‮⁠-⁯﻿]")


# -- the person's configuration ---------------------------------------------------------------------------------------


@dataclass
class _Person:
    """What the person's config says that a checkout needs. ``known`` is false when it cannot be read with certainty."""

    known: bool = True
    why: str = ""
    filters: dict[str, dict[str, str | None]] = field(default_factory=dict)
    autocrlf: str | None = None
    eol: str | None = None
    attributes_file: str | None = None


def _person(env: Mapping[str, str]) -> _Person:
    p = _Person()
    home = env.get("HOME") or ""
    if not home.startswith("/"):
        return _Person(False, "HOME is not an absolute path")
    xdg = env.get("XDG_CONFIG_HOME") or os.path.join(home, ".config")
    if not xdg.startswith("/"):
        return _Person(False, "XDG_CONFIG_HOME is not an absolute path")
    files = [*SYSTEM_CONFIGS, os.path.join(xdg, "git", "config"), os.path.join(home, ".gitconfig")]
    for path in files:
        if not os.path.lexists(path):
            continue
        data = _read_small(path, limits.CONFIG_MAX)
        if data is None:
            return _Person(False, f"{path} cannot be read")
        try:
            entries = parse(data)
        except Unavailable:
            return _Person(False, f"{path} cannot be parsed")
        for section, sub, key, value in entries:
            if section in ("include", "includeif"):
                return _Person(False, f"{path} has an include")
            if section == "filter" and sub is not None:
                p.filters.setdefault(sub.lower(), {})[key] = value
            elif section == "core" and sub is None:
                if key == "autocrlf":
                    p.autocrlf = "true" if value is None else value.strip().lower()
                elif key == "eol":
                    p.eol = (value or "").lower()
                elif key == "attributesfile":
                    p.attributes_file = value
    return p


# -- attributes -------------------------------------------------------------------------------------------------------

_ATTR_NAME = re.compile(r"^[A-Za-z0-9_.][A-Za-z0-9_.-]*$")


@dataclass
class _Rule:
    glob: Glob | None
    slash: bool
    attrs: list[tuple[str, str, str | None]]  # (name, "set"|"unset"|"unspec"|"val", value)


def _expand(
    attrs: list[tuple[str, str, str | None]], macros: dict[str, list[tuple[str, str, str | None]]]
) -> list[tuple[str, str, str | None]]:
    """One level of macro expansion. Macros are stored already expanded (:class:`_AttrFile` expands each at its
    definition against the macros defined before it, as git does), so this never recurses and the result is bounded by
    :data:`ATTRS_MAX` (more is :class:`Unavailable`)."""
    out: list[tuple[str, str, str | None]] = []
    for name, state, val in attrs:
        macro = macros.get(name)
        if macro is not None:
            if state != "set":
                raise Unavailable("a macro attribute is unset or reset; this reader cannot be sure")
            out.extend(macro)
        out.append((name, state, val))
        if len(out) > ATTRS_MAX:
            raise Unavailable("a .gitattributes line expands to too many attributes")
    return out


def _tokens(words: list[str]) -> list[tuple[str, str, str | None]]:
    out: list[tuple[str, str, str | None]] = []
    for w in words:
        if w.startswith("-") and _ATTR_NAME.match(w[1:]):
            out.append((w[1:], "unset", None))
        elif w.startswith("!") and _ATTR_NAME.match(w[1:]):
            out.append((w[1:], "unspec", None))
        elif "=" in w and _ATTR_NAME.match(w.split("=", 1)[0]):
            n, _, v = w.partition("=")
            out.append((n, "val", v))
        elif _ATTR_NAME.match(w):
            out.append((w, "set", None))
        else:
            raise Unavailable("a .gitattributes line has an attribute this reader cannot be sure of")
    return out


class _AttrFile:
    def __init__(
        self, base: bytes, data: bytes, macros: dict[str, list[tuple[str, str, str | None]]], root: bool
    ) -> None:
        self.base = base  # the directory the file is in, relative to the worktree root (b"" or b"a/b/")
        self.rules: list[_Rule] = []
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            raise Unavailable("a .gitattributes file is not UTF-8") from None
        if text.startswith("﻿"):
            text = text[1:]
        for line in text.split("\n"):
            line = line.strip(" \t\r")
            if not line or line.startswith("#"):
                continue
            if line.startswith('"'):
                raise Unavailable("a quoted .gitattributes pattern is not supported")
            words = line.split()
            pat, rest = words[0], words[1:]
            if pat.startswith("[attr]"):
                if root and len(pat) > 6:
                    macros[pat[6:]] = _expand(_tokens(rest), macros)
                continue
            if pat.startswith("!") or pat.endswith("/"):
                continue  # git never matches a file with these
            slash = "/" in pat
            glob = compile_glob(pat.lstrip("/").encode("utf-8"))
            expanded = [t for t in _expand(_tokens(rest), macros) if t[0] in _WANT]  # only what a checkout acts on
            if expanded:
                self.rules.append(_Rule(glob, slash, expanded))


class _Attrs:
    """The ``filter``, ``ident``, ``working-tree-encoding`` and ``eol`` attributes of a path, from git's sources in
    git's order: ``info/attributes`` (highest), the tree's ``.gitattributes`` files (deepest first), the person's
    global attributes file."""

    def __init__(self, info: _AttrFile | None, global_: _AttrFile | None, macros: dict) -> None:
        self.info, self.global_, self.macros = info, global_, macros

    def query(self, stack: list[_AttrFile], rel: bytes) -> dict[str, tuple[str, str | None]]:
        sources = [self.info, *reversed(stack), self.global_]
        result: dict[str, tuple[str, str | None]] = {}
        for src in sources:
            if src is None:
                continue
            sub = rel[len(src.base) :]
            base_name = sub.rsplit(b"/", 1)[-1]
            for rule in reversed(src.rules):
                if rule.glob is None or all(w in result for w in _WANT):
                    continue
                if not rule.glob.match(sub if rule.slash else base_name):
                    continue
                for name, state, val in reversed(rule.attrs):
                    if name in _WANT and name not in result:
                        result[name] = (state, val)
        return result


# -- the writer -------------------------------------------------------------------------------------------------------


def _fold(raw: bytes) -> str | None:
    """The spelling a case/Unicode-folding file system or git's short-name rules would compare: NFKC, ignorable code
    points dropped, casefolded, trailing dots and spaces removed. ``None`` for a non-UTF-8 name."""
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return None
    return _IGNORABLE.sub("", unicodedata.normalize("NFKC", text)).casefold().rstrip(" .")


#: names git refuses to check out as anything but a regular file (``fsck`` ``*_SYMLINK``), with their 8.3 short names
_REGULAR_ONLY = {".gitmodules": "gitmod~", ".gitattributes": "gitatt~", ".gitignore": "gitign~", ".mailmap": "mailma~"}


def _special_symlink(raw: bytes) -> bool:
    s = _fold(raw)
    if s is None:
        return False
    return any(s == n or (s.startswith(short) and s[len(short) :].isdigit()) for n, short in _REGULAR_ONLY.items())


def _bad_name(raw: bytes) -> bool:
    """git's ``verify_path`` for one component, plus the spellings other file systems fold to ``.git``."""
    if not raw or b"\0" in raw or b"/" in raw or raw in (b".", b".."):
        return True
    s = _fold(raw)
    if s is None:
        return False  # a non-UTF-8 name is a plain byte name; it cannot spell .git on a folding file system
    return s == ".git" or (s.startswith("git~") and s[4:].isdigit())


@dataclass
class _Entry:
    path: bytes
    mode: int
    oid: str
    st: os.stat_result


class _Writer:
    def __init__(
        self,
        repo: Repo,
        root_dir: Dir,
        person: _Person,
        attrs: _Attrs,
        env: Mapping[str, str],
        cwd: str,
        log: Callable[[str], None] | None,
    ) -> None:
        self.repo, self.person, self.attrs, self.env, self.cwd, self.log = repo, person, attrs, env, cwd, log
        self.deadline = time.monotonic() + POPULATE_SECONDS
        self.entries: list[_Entry] = []
        self.count = 0
        self.root_dir = root_dir
        self.macros = attrs.macros

    def walk(self, dirfd: int, tree: str, prefix: bytes, stack: list[_AttrFile], depth: int = 0) -> None:
        if depth > DEPTH_MAX:
            raise Unavailable("the tree is nested too deep")
        items = self.repo.tree(tree)
        names = {e.name for e in items}
        if len(names) != len(items):
            raise Unavailable("a tree lists one name twice")
        for e in items:
            if _bad_name(e.name):
                raise Unavailable("the tree has a path git itself refuses (a .git component or a bad name)")
        for e in items:
            if _fold(e.name) == ".gitattributes" and e.mode in (0o100644, 0o100755):
                blob = self.repo.blob(e.oid)
                if len(blob) > ATTR_FILE_MAX:
                    raise Unavailable("a .gitattributes file is larger than 1 MiB")
                stack = [*stack, _AttrFile(prefix, blob, self.macros, depth == 0)]
        for e in items:
            self.count += 1
            if time.monotonic() > self.deadline:
                raise Unavailable("the checkout took too long")
            if self.count > limits.TREE_ENTRIES_MAX:
                raise Unavailable("too many paths to check out")
            path = prefix + e.name
            if e.mode == 0o040000:
                os.mkdir(e.name, 0o777, dir_fd=dirfd)
                sub = os.open(e.name, os.O_RDONLY | _DIRECTORY | _NOFOLLOW | _CLOEXEC, dir_fd=dirfd)
                try:
                    self.walk(sub, e.oid, path + b"/", stack, depth + 1)
                finally:
                    os.close(sub)
                continue
            if e.mode == 0o160000:
                os.mkdir(e.name, 0o777, dir_fd=dirfd)  # a submodule: an empty directory, as git without --recurse
            elif e.mode == 0o120000:
                if _special_symlink(e.name):
                    raise Unavailable("a .gitmodules, .gitattributes, .gitignore or .mailmap is a symlink")
                target = self.repo.blob(e.oid)
                if not target or b"\0" in target:
                    raise Unavailable("a symlink has an empty or malformed target")
                os.symlink(target, e.name, dir_fd=dirfd)
            else:
                data = self.repo.blob(e.oid)
                data = self._convert(path, data, stack)
                fd = os.open(
                    e.name,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL | _NOFOLLOW | _CLOEXEC,
                    0o777 if e.mode == 0o100755 else 0o666,
                    dir_fd=dirfd,
                )
                try:
                    view = memoryview(data)
                    while view:
                        view = view[os.write(fd, view) :]
                finally:
                    os.close(fd)
            st = os.stat(e.name, dir_fd=dirfd, follow_symlinks=False)
            self.entries.append(_Entry(path, e.mode, e.oid, st))

    def _convert(self, path: bytes, data: bytes, stack: list[_AttrFile]) -> bytes:
        q = self.attrs.query(stack, path)
        if q.get("ident", ("unspec", None))[0] in ("set", "val"):
            raise Unavailable("a path has the ident attribute; this checkout does not convert it")
        enc = q.get("working-tree-encoding", ("unspec", None))
        if enc[0] != "unspec" and enc[0] != "unset" and (enc[1] or "").lower().replace("-", "") != "utf8":
            raise Unavailable("a path has a working-tree-encoding; this checkout does not convert it")
        eol = q.get("eol", ("unspec", None))
        if eol[0] == "val" and (eol[1] or "").lower() == "crlf":
            raise Unavailable("a path has eol=crlf; this checkout does not convert it")
        flt = q.get("filter", ("unspec", None))
        if flt[0] != "val" or not flt[1]:
            return data
        raise Unavailable(
            "this repo uses Git LFS / filters; worktrees for it are not supported yet "
            f"(a path selects the filter {flt[1]!r})"
        )


def _index_bytes(entries: list[_Entry], algo: str) -> bytes:
    h = hashlib.sha256 if algo == "sha256" else hashlib.sha1
    out = [b"DIRC", struct.pack(">II", 2, len(entries))]
    for e in sorted(entries, key=lambda x: x.path):
        st = e.st
        u = lambda v: v & 0xFFFFFFFF  # noqa: E731
        head = struct.pack(
            ">10I",
            u(int(st.st_ctime)), u(st.st_ctime_ns % 1_000_000_000), u(int(st.st_mtime)),
            u(st.st_mtime_ns % 1_000_000_000), u(st.st_dev), u(st.st_ino), e.mode, u(st.st_uid), u(st.st_gid),
            u(st.st_size) if stat.S_ISREG(st.st_mode) or stat.S_ISLNK(st.st_mode) else 0,
        )  # fmt: skip
        raw = bytes.fromhex(e.oid)
        flags = struct.pack(">H", min(len(e.path), 0xFFF))
        fixed = len(head) + len(raw) + 2
        pad = ((fixed + len(e.path) + 8) & ~7) - fixed - len(e.path)
        out.append(head + raw + flags + e.path + b"\0" * pad)
    body = b"".join(out)
    return body + h(body).digest()


def _write_index(gitdir: Dir, data: bytes) -> None:
    if gitdir.kind("index") is not None:
        raise Unavailable("the new worktree already has an index")
    fd = os.open("index.lock", os.O_WRONLY | os.O_CREAT | os.O_EXCL | _NOFOLLOW | _CLOEXEC, 0o666, dir_fd=gitdir.fd)
    try:
        try:
            view = memoryview(data)
            while view:
                view = view[os.write(fd, view) :]
        finally:
            os.close(fd)
        os.rename("index.lock", "index", src_dir_fd=gitdir.fd, dst_dir_fd=gitdir.fd)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink("index.lock", dir_fd=gitdir.fd)
        raise


def _load_attr_file(path: str, macros: dict) -> _AttrFile | None:
    if not os.path.lexists(path):
        return None
    data = _read_small(path, ATTR_FILE_MAX)
    if data is None:
        raise Unavailable(f"{path} cannot be read")
    return _AttrFile(b"", data, macros, True)


def populate(path: str, env: Mapping[str, str], *, log: Callable[[str], None] | None = None) -> None:
    """Write the tree of the commit ``HEAD`` names into the empty (``--no-checkout``) linked worktree at ``path``,
    then its index. Raises :class:`Unavailable` for anything it cannot write exactly; the caller removes the worktree.
    """
    pins = pin_repo(path, None)
    person = _person(env)
    if person.autocrlf not in (None, "", "false", "no", "off", "0", "input") or person.eol == "crlf":
        raise Unavailable("the person's core.autocrlf/core.eol would convert line endings; not supported here")
    with Repo(pins) as repo:
        ref, detached = read_head_any(repo.gitdir, repo.hexlen)
        commit = detached if ref is None else repo.resolve_commit(ref)
        if commit is None:
            raise Unavailable("HEAD names no commit")
        tree = repo.commit(commit).tree
        macros: dict[str, list[tuple[str, str, str | None]]] = {}
        gattr = None
        if person.attributes_file:
            gp = person.attributes_file
            gp = os.path.join(env.get("HOME", ""), gp[2:]) if gp.startswith("~/") else gp
            if not gp.startswith("/"):
                raise Unavailable("core.attributesFile is not an absolute path")
            gattr = _load_attr_file(gp, macros)
        else:
            xdg = env.get("XDG_CONFIG_HOME") or os.path.join(env.get("HOME", ""), ".config")
            gattr = _load_attr_file(os.path.join(xdg, "git", "attributes"), macros)
        raw_info = (
            repo.commondir.read_optional("info/attributes", ATTR_FILE_MAX)
            if repo.commondir.kind("info") == "dir"
            else None
        )
        info = None if raw_info is None else _AttrFile(b"", raw_info, macros, True)
        attrs = _Attrs(info, gattr, macros)
        root = open_pin(pins.worktree)
        try:
            if set(root.listdir()) != {".git"}:
                raise Unavailable("the new worktree is not empty")
            w = _Writer(repo, root, person, attrs, env, pins.worktree.path, log)
            w.walk(root.fd, tree, b"", [])
            _write_index(repo.gitdir, _index_bytes(w.entries, repo.algo))
        finally:
            root.close()
