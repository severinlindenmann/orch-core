"""The one path-containment and deny-list module of the File Explorer (issue #491, owner decisions 5 and 6).

**Every read of the file system by the explorer goes through this module** (tree, file preview, raw images, the
filename walk, the paths of search hits and of git history). Nothing else in `orch_dashboard.explorer` calls
`open`, `scandir`, `stat` or `realpath` on a path that came from a request. This is the single place to harden, and
the single place a later move to host-mediated `fs.*` ops replaces.

**Containment.**
* A path is always `(root, relative path)`. The relative path is split into components here; an empty component, `.`,
  `..`, a NUL, a `/` or a backslash inside a component is refused (`Invalid`). It is never joined into a string path.
* A root is a folder the server itself chose (the workspace folder from the host registry, or a repository declared in
  `settings.repos`), made absolute with `realpath` once, and must be a folder owned by the person, not `/`, not the
  home folder and not inside the orch state directory (`Root.open`). Every later step goes below the root's file
  descriptor.
* Descent is one component at a time with `openat` (`dir_fd`) and `O_NOFOLLOW`: **no symlink is ever followed**, at
  any depth, so a link cannot lead out of the root. A link is shown in the tree as a link and cannot be opened.
* A directory whose identity `(st_dev, st_ino)` is the orch state directory, the run directory or the workspace
  store is refused whatever its name or path (a bind mount or a hard-linked name cannot sneak it in).

**Deny list** (`deny`; one list, applies to every root, case-insensitive because macOS volumes are):
git internals (`.git`, at any depth, file or folder), the orch store (`.orch` at any depth), key material
(`*.pem *.key *.p8 *.p12 *.pfx`, `id_rsa`-style private keys, `.ssh`, `.gnupg`, `.netrc`, `keys.jsonl`) and every
`.env*` file. The names live in `orch.gitbrowse` and git excludes the same ones with pathspecs.
A denied entry is **shown locked** in its folder (name and kind of lock, no size, no content); reading, listing,
searching or asking for the history of a denied path is `Denied`.

A file or folder name is data: its label for the page replaces invisible or control characters with visible
`\\uXXXX` text (`label`), never hides them.
"""

from __future__ import annotations

import contextlib
import os
import stat
from collections.abc import Iterator
from dataclasses import dataclass, field
from functools import cached_property

from orch import gitbrowse

from ..text import hidden

_FLAGS = getattr(os, "O_CLOEXEC", 0)
_DIR = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | _FLAGS
_FILE = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | _FLAGS

#: folder entries listed per folder; a larger folder says so
MAX_ENTRIES = 3000
#: entries the walk of orch's own folders visits before it gives up (then every multi-link file is locked)
WALK_LIMIT = 50_000
MAX_PATH_PARTS = 64
MAX_PART = 255


class GuardError(Exception):
    """Base of the refusals; `reason` is plain words for the person."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


class Invalid(GuardError):
    """The path is not a plain relative path."""


class Denied(GuardError):
    """The path is on the deny list (locked)."""


class Missing(GuardError):
    """There is nothing there (or it is a link, which is never followed)."""


class Unusable(GuardError):
    """The root is not a folder the explorer may open."""


def deny(parts: tuple[str, ...]) -> str | None:
    """Why this relative path is locked (a short plain phrase), or None. The empty path (the root itself) is never
    locked. The names are :data:`orch.gitbrowse.DENIED_NAMES` and friends: the same list git excludes with pathspecs."""
    for index, part in enumerate(parts):
        low = part.casefold()
        if low == ".git":
            return "Git internals"
        if low in (".orch", ".state"):  # the store, and orch's local state wherever a folder of that name is
            return "orch's own files"
        if low == ".env" or low.startswith(".env."):
            return "Secrets (.env)"
        if low.endswith(".worktrees"):
            return "Ticket worktrees"
        if gitbrowse.denied_name(part):
            return "Keys"
    return None


def label(name: str) -> str:
    """A name for display: invisible, bidi and control characters become visible ``\\uXXXX`` text."""
    if not hidden(name):
        return name
    return "".join(f"\\u{ord(c):04x}" if hidden(c) else c for c in name)


def split(rel: str) -> tuple[str, ...]:
    """The components of a relative path as the page sends it (``a/b/c``; ``""`` is the root). Plain or `Invalid`."""
    if not isinstance(rel, str) or len(rel) > 4096 or "\0" in rel or "\\" in rel:
        raise Invalid("That is not a path inside this workspace.")
    if rel == "":
        return ()
    parts = tuple(rel.split("/"))
    if len(parts) > MAX_PATH_PARTS or any(p in ("", ".", "..") or len(p) > MAX_PART for p in parts):
        raise Invalid("That is not a path inside this workspace.")
    return parts


def _identity(st: os.stat_result) -> tuple[int, int]:
    return (st.st_dev, st.st_ino)


def _inside(path: str, folder: str) -> bool:
    return path == folder or path.startswith(folder.rstrip(os.sep) + os.sep)


@dataclass(frozen=True)
class Root:
    """A folder the explorer may show. ``id``: ``"ws"`` for the workspace folder, ``"repo:<name>"`` for a declared
    repository; ``path``: absolute and real; ``forbidden``: real paths whose folders (by identity) can never be
    opened."""

    id: str
    label: str
    path: str
    kind: str
    forbidden: tuple[str, ...] = field(default=())

    def _banned(self) -> set[tuple[int, int]]:
        found = set()
        for p in self.forbidden:
            with contextlib.suppress(OSError):
                found.add(_identity(os.stat(p)))
        return found

    @cached_property
    def _banned_files(self) -> tuple[set[tuple[int, int]], bool]:
        """``(identities, complete)``: the files of EVERY orch-owned place of the workspace (the state and run folders
        and the store folder), found by walking them without a depth limit but with an entry limit
        (:data:`WALK_LIMIT`). ``complete`` is False when the limit was reached or a folder could not be read: the
        caller then treats every file with more than one name as locked (fail closed). A HARD LINK to one of these
        files, made under any name anywhere, is the same file."""
        found: set[tuple[int, int]] = set()
        complete = True
        seen = 0
        starts = set(self.forbidden)
        # a starting place may itself be a link (a symlinked config folder): follow THAT, once, and only that; links met
        # inside the walk are never followed. Each real place once.
        stack = sorted({os.path.realpath(p) for p in starts})
        top = set(stack)
        while stack:
            path = stack.pop()
            if seen >= WALK_LIMIT:
                return found, False
            try:
                st = os.lstat(path)
            except FileNotFoundError:
                continue
            except OSError:
                complete = False
                continue
            seen += 1
            if stat.S_ISREG(st.st_mode):
                found.add(_identity(st))
            elif path in top and not stat.S_ISDIR(st.st_mode):
                complete = False  # a starting place that is neither a folder nor a file: not provably covered
            elif stat.S_ISDIR(st.st_mode):
                try:
                    with os.scandir(path) as it:
                        stack += [e.path for e in it]
                except OSError:
                    complete = False
        return found, complete

    def _check_links(self, st: os.stat_result) -> None:
        """THE hard-link check: a regular file with more than one name that is (or, when the walk was incomplete, may
        be) one of orch's own files is `Denied`. Every path that returns or searches content calls this through
        :meth:`stat_file` or :meth:`read`."""
        if stat.S_ISREG(st.st_mode) and st.st_nlink > 1:
            ids, complete = self._banned_files
            if not complete or _identity(st) in ids:
                raise Denied("orch's own files")

    def _open_root(self) -> int:
        """The root folder as a descriptor, after the checks that make it a folder the explorer may show."""
        real = os.path.realpath(self.path)
        home = os.path.realpath(os.path.expanduser("~"))
        if real in ("/", home) or any(_inside(real, os.path.realpath(f)) for f in self.forbidden):
            raise Unusable("This folder cannot be shown.")
        try:
            fd = os.open(real, _DIR)
        except OSError:
            raise Unusable("This folder is missing or cannot be opened.") from None
        st = os.fstat(fd)
        if st.st_uid != os.getuid() or _identity(st) in self._banned():
            os.close(fd)
            raise Unusable("This folder cannot be shown.")
        return fd

    @contextlib.contextmanager
    def dir(self, parts: tuple[str, ...]) -> Iterator[int]:
        """The folder ``parts`` below the root as an open descriptor (closed on exit). `Denied` for a locked path,
        `Missing` when a component is absent, a link or not a folder."""
        fds: list[int] = []
        try:
            if reason := deny(parts):
                raise Denied(reason)
            fd = self._open_root()
            fds.append(fd)
            banned = self._banned()
            for part in parts:
                try:
                    fd = os.open(part, _DIR, dir_fd=fd)
                except OSError:
                    raise Missing("There is no such folder (or it is a link).") from None
                fds.append(fd)
                if _identity(os.fstat(fd)) in banned:
                    raise Denied("orch's own files")
            yield fd
        finally:
            for f in reversed(fds):
                os.close(f)

    def entries(self, parts: tuple[str, ...]) -> tuple[list[dict], bool]:
        """The entries of the folder: ``(rows, more)``. A row: ``name``, ``label``, ``kind`` (``dir``, ``file``,
        ``link``, ``other``), ``size`` (files), ``repo`` (a folder with its own ``.git``), ``locked`` (the reason, or
        None). Folders first, then by name. At most :data:`MAX_ENTRIES` rows; ``more`` says it was cut."""
        rows: list[dict] = []
        more = False
        with self.dir(parts) as fd:
            banned = self._banned()
            with os.scandir(fd) as it:
                for entry in it:
                    if len(rows) >= MAX_ENTRIES:
                        more = True
                        break
                    name = entry.name
                    row = {"name": name, "label": label(name), "locked": deny((*parts, name))}
                    try:
                        st = entry.stat(follow_symlinks=False)
                    except OSError:
                        continue
                    mode = st.st_mode
                    if stat.S_ISDIR(mode):
                        row["kind"] = "dir"
                        if row["locked"] is None and _identity(st) in banned:
                            row["locked"] = "orch's own files"
                        row["repo"] = False
                        if row["locked"] is None:
                            # only a yes/no: `name` is a real folder (lstat above); nothing inside is read
                            with contextlib.suppress(OSError):
                                row["repo"] = stat.S_ISDIR(
                                    os.stat(f"{name}/.git", dir_fd=fd, follow_symlinks=False).st_mode
                                )
                    elif stat.S_ISREG(mode):
                        row["kind"] = "file"
                        row["size"] = st.st_size
                    elif stat.S_ISLNK(mode):
                        row["kind"] = "link"
                    else:
                        row["kind"] = "other"
                    if row["locked"] is not None:
                        row.pop("size", None)
                        row["repo"] = False
                    rows.append(row)
        rows.sort(key=lambda r: (r["kind"] != "dir", r["name"].casefold(), r["name"]))
        return rows, more

    def read(self, parts: tuple[str, ...], limit: int) -> tuple[bytes, os.stat_result, bool]:
        """Up to ``limit`` bytes of a regular file: ``(data, stat, cut)``. Never follows a link, never opens a device or
        a pipe, `Denied` for a locked path, `Missing` for a link, a folder or an absent file."""
        if not parts:
            raise Invalid("Choose a file.")
        if reason := deny(parts):
            raise Denied(reason)
        with self.dir(parts[:-1]) as fd:
            try:
                f = os.open(parts[-1], _FILE, dir_fd=fd)
            except OSError:
                raise Missing("There is no such file (or it is a link).") from None
            try:
                st = os.fstat(f)
                if not stat.S_ISREG(st.st_mode):
                    raise Missing("This is not a regular file.")
                self._check_links(st)
                data = b""
                while len(data) <= limit:
                    chunk = os.read(f, min(65536, limit + 1 - len(data)))
                    if not chunk:
                        break
                    data += chunk
            finally:
                os.close(f)
        return data[:limit], st, len(data) > limit

    def stat_file(self, parts: tuple[str, ...]) -> os.stat_result:
        """``lstat`` of a path below the root (no link followed), after the same checks as :meth:`read`."""
        if not parts:
            raise Invalid("Choose a file.")
        if reason := deny(parts):
            raise Denied(reason)
        with self.dir(parts[:-1]) as fd:
            try:
                st = os.stat(parts[-1], dir_fd=fd, follow_symlinks=False)
            except OSError:
                raise Missing("There is no such file.") from None
            self._check_links(st)
            return st

    #: files that move where git reads its objects or its history from: a repository that has one is not used
    _REDIRECTS = (
        ".git/commondir",
        ".git/objects/info/alternates",
        ".git/objects/info/http-alternates",
        ".git/info/grafts",
    )

    def has_git(self, parts: tuple[str, ...]) -> bool:
        """Does the folder ``parts`` hold its own, plain git working copy? A real ``.git`` FOLDER (not a link) with a
        real ``objects`` folder (not a link) and none of the files that redirect git: ``commondir``,
        ``objects/info/alternates``, ``objects/info/http-alternates``, ``info/grafts``. A ``.git`` FILE
        (``gitdir: <elsewhere>``) is refused: it can send git to a git directory outside the root or inside a forbidden
        folder. The caller (`git.locate`) additionally asks git where it finds its directories and requires exactly
        these, and pins git to them."""
        try:
            with self.dir(parts) as fd:
                for name in (".git", ".git/objects"):
                    if not stat.S_ISDIR(os.stat(name, dir_fd=fd, follow_symlinks=False).st_mode):
                        return False
                for name in self._REDIRECTS:
                    try:
                        os.stat(name, dir_fd=fd, follow_symlinks=False)
                    except FileNotFoundError:
                        continue
                    return False  # present (or unreadable: not provably plain)
                return True
        except (OSError, GuardError):
            return False

    def abs(self, parts: tuple[str, ...]) -> str:
        """The absolute path of ``parts`` below the root, for a program that needs one. Only call it after
        :meth:`dir` or :meth:`stat_file` accepted the same parts (no link, not denied)."""
        return os.path.join(self.path, *parts)

    def walk_names(
        self, parts: tuple[str, ...], limit: int, skip: frozenset[str]
    ) -> tuple[list[tuple[str, ...]], bool]:
        """Paths of files below a folder, breadth first, at most ``limit`` visited entries; locked paths and links are
        left out and the folders named in ``skip`` (``node_modules``) are not entered. ``(paths, cut)``."""
        found: list[tuple[str, ...]] = []
        queue = [parts]
        seen = 0
        while queue:
            here = queue.pop(0)
            try:
                rows, more = self.entries(here)
            except GuardError:
                continue
            for row in rows:
                seen += 1
                if seen > limit:
                    return found, True
                if row["locked"] is not None:
                    continue
                path = (*here, row["name"])
                if row["kind"] == "dir":
                    if row["name"] not in skip:
                        queue.append(path)
                elif row["kind"] == "file":
                    found.append(path)
            if more:
                return found, True
        return found, False


def exclude_globs() -> list[str]:
    """The deny list as ripgrep ``--glob`` exclusions (keeps a search from READING locked files; every hit is also
    re-checked with :func:`deny`, so a glob that misses something cannot leak it)."""
    out = [f"!{g}" for g in gitbrowse.DENY_GLOBS] + [f"!{g}/**" for g in gitbrowse.DENY_GLOBS]
    out += ["!.state", "!.state/**"]
    return out
