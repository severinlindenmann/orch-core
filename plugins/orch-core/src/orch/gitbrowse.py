"""Read-only git for the File Explorer (issue #491): history of one file, one commit's patch, a file at a revision,
the difference of two versions, the files a commit changed, and a literal content search.

**This module adds no way around :mod:`orch.gitsafe`; it is a second CLOSED set of operations on top of it**, for
callers that need a log, a patch of one commit, a file's content at a revision, or a search (the set in
``gitsafe.OPS`` is the one the host's own observation uses and stays as it is). Every operation:

* runs :func:`orch.gitsafe.gate` first, every time (allow-listed repository config, config files owned by the person,
  no program a config key could name), then builds its argv from a FIXED template: :data:`orch.gitsafe.HARDEN` (hooks
  to ``/dev/null``, no fsmonitor, no network protocol, no submodule recursion, no attributes file, no commit graph),
  ``--no-pager``, ``--no-optional-locks``, ``-C <repo>``, and for ``log``/``show``/``diff`` ``--no-ext-diff
  --no-textconv`` plus the diff pins of ``gitsafe`` (every ``diff.*`` knob, every diff driver's ``binary``/
  ``xfuncname``/``funcname``);
* takes only validated operands, each in a slot of the template: ``<commit>`` (``HEAD``, a full id or
  ``refs/heads/<name>``, written ``<rev>^{commit}`` so git refuses a tree, a blob or a tag; always after
  ``--end-of-options``, or the front of ``<commit>:./<file>``), ``<file>`` (a path below the repository: no leading
  ``/``, no ``.``/``..``/empty component, no control characters; always behind ``:(literal)`` after ``--``, or in the
  object name ``<commit>:./<file>``), ``<num>`` (digits) and ``<pattern>`` (a literal search text, always the value of
  ``-e``). No caller can add a global option, a subcommand or an option of its own;
* is pinned to the folders the caller confirmed (:func:`pins`: ``GIT_DIR``, ``GIT_COMMON_DIR``, ``GIT_WORK_TREE``,
  ``GIT_OBJECT_DIRECTORY``; the ``layout`` operation, which asks git where it would look, runs unpinned);
* runs with the person's system and global git configuration OFF (:func:`orch.gitsafe.isolated_env`), a scratch
  environment (no ``GIT_*`` of the caller), ``cwd="/"``, stdin closed, in its own process group that is killed after
  ``timeout`` seconds or when the output reaches ``max_bytes`` (:func:`run`);
* cannot reach the network (no fetch/pull/push/clone subcommand exists here); ``grep`` searches the index
  (``--cached``: the working tree is never opened, so no symlink there is followed) and never enters a submodule;
* excludes the explorer's deny list (:data:`DENY_SPECS`, plus the store names the caller passes as ``exclude_top``)
  with ``:(exclude)`` pathspecs on every operation that takes pathspecs, on top of the caller's own path check.

Residual (documented, same as ``gitsafe``): a ``filter`` driver the PERSON configured globally is never run here (the
global configuration is off and these operations never check a file out).
"""

from __future__ import annotations

import contextlib
import os
import re
import signal
import subprocess
import time
from collections.abc import Mapping
from dataclasses import dataclass

from orch import fdwait, gitsafe
from orch.gitsafe import Unsafe
from orch.layout import KEYS_FILE, STATE_DIR, STORE_DIR

__all__ = [
    "OPS",
    "Result",
    "command",
    "parse_dir_log",
    "parse_grep",
    "parse_log",
    "parse_names",
    "parse_show",
    "parse_tree",
    "run",
    "spawn",
]

_SLOT = re.compile(r"<(commit|file|num|pattern)>")

#: THE deny list of the File Explorer, by name (compared lower-case, at any depth). The dashboard's path guard
#: (`orch_dashboard.explorer.guard`) imports these, and every git operation below excludes the same names with
#: ``:(exclude)`` pathspecs ON TOP of the guard's own check (never instead of it), so a path the guard missed still
#: cannot come out of git. Git internals, orch's store, key material and ``.env`` files.
DENIED_NAMES = frozenset({
    ".git", STORE_DIR, ".ssh", ".gnupg", ".netrc", KEYS_FILE, ".env", ".git-credentials", ".npmrc", ".pypirc", ".aws",
    ".docker", ".kube", STATE_DIR,
})  # fmt: skip
DENIED_PREFIXES = (".env.", "id_rsa", "id_dsa", "id_ecdsa", "id_ed25519")
DENIED_SUFFIXES = (".pem", ".key", ".p8", ".p12", ".pfx", ".keystore", ".jks", ".kdbx", ".worktrees")
DENIED_CONTAINS = (".keychain",)


def denied_name(component: str) -> bool:
    """Is this ONE path component on the deny list (any depth; a denied FOLDER locks everything below it)?"""
    low = component.casefold()
    return (
        low in DENIED_NAMES
        or low.startswith(DENIED_PREFIXES)
        or low.endswith(DENIED_SUFFIXES)
        or any(c in low for c in DENIED_CONTAINS)
    )


_G = ":(exclude,glob,icase)"
#: the same list as glob patterns of ONE component; each is excluded as a file/folder AND as everything below it
DENY_GLOBS = (
    *sorted(DENIED_NAMES),
    *(f"{p}*" for p in DENIED_PREFIXES),
    *(f"*{s}" for s in DENIED_SUFFIXES),
    *(f"*{c}*" for c in DENIED_CONTAINS),
)
#: pathspecs appended to every operation that takes pathspecs (after ``--``)
DENY_SPECS = (*(f"{_G}**/{g}" for g in DENY_GLOBS), *(f"{_G}**/{g}/**" for g in DENY_GLOBS))
_TOP_NAME = re.compile(r"[A-Za-z0-9._-]{1,100}")
#: pins on top of HARDEN for these operations (command scope beats every config file)
_EXTRA = (
    "-c", "i18n.logOutputEncoding=UTF-8", "-c", "core.quotePath=false", "-c", "log.showSignature=false",
    "-c", "log.mailmap=false", "-c", "log.follow=false", "-c", "log.showRoot=true", "-c", "log.decorate=no",
    "-c", "grep.patternType=fixed", "-c", "grep.extendedRegexp=false", "-c", "grep.fallbackToNoIndex=false",
)  # fmt: skip
#: the record format of ``log``/``show``: NUL, commit id, NUL, author name, NUL, author date (strict ISO 8601), NUL,
#: subject (and for ``show``: NUL, body, NUL). A commit message cannot hold a NUL, so a message can never end a field.
_COMMIT = "%x00%H%x00%aN%x00%aI%x00%s"
_BODY = _COMMIT + "%x00%b%x00"
#: ``dir_log`` names the author with ``%an``, not ``%aN``: ``%aN`` applies the repository's ``.mailmap`` FROM THE WORK
#: TREE (and ``mailmap.file``), so an uncommitted edit would change who a committed page is attributed to
_COMMIT_RAW = "%x00%H%x00%an%x00%aI%x00%s"
#: most files one operation takes in its ``<files>`` slot
MAX_FILES = 1000


@dataclass(frozen=True)
class Op:
    args: tuple[str, ...]

    @property
    def operands(self) -> tuple[str, ...]:
        return tuple(m for a in self.args for m in _SLOT.findall(a))


_SHOW = ("--no-color", "--no-renames", "--root", "-m", "--first-parent")
OPS: dict[str, Op] = {
    # the commits that changed one file, newest first (see _COMMIT)
    "file_log": Op(("log", "--no-color", "--no-renames", f"--format={_COMMIT}", "--max-count=<num>",
                    "--skip=<num>", "--end-of-options", "<commit>", "--", ":(literal)<file>")),
    # one commit's patch for one file (a merge: against its first parent), with the commit's record first
    "commit_file_patch": Op(("show", *_SHOW, f"--format={_BODY}", "-U<num>", *gitsafe._DIFF_FIXED,
                             "--end-of-options", "<commit>", "--", ":(literal)<file>")),
    # the files one commit changed: `<status> NUL <path> NUL ...`
    "commit_names": Op(("show", *_SHOW, "--format=", "--name-status", "-z", *gitsafe._DIFF_FIXED, "--end-of-options",
                        "<commit>", "--", ":(top)")),
    # where git itself finds its directories for `repo`: git dir, common dir, top, objects (checked by the caller)
    "layout": Op(("rev-parse", "--path-format=absolute", "--git-dir", "--git-common-dir", "--show-toplevel",
                  "--git-path", "objects")),
    # the newest change to a path, as `<status> NUL <path> NUL`: only FILES are listed, so a folder (or a name that was
    # never a file) has no row with exactly its own path
    "last_change": Op(("log", "--max-count=1", "--format=", "--name-status", "-z", "--no-renames", "--end-of-options",
                       "<commit>", "--", ":(literal)<file>")),
    # the files that differ between two commits (old, new): `<status> NUL <path> NUL ...`
    "range_names": Op(("diff", "--no-color", "--no-renames", "--name-status", "-z", *gitsafe._DIFF_FIXED,
                       "--end-of-options", "<commit>", "<commit>", "--", ":(top)")),
    # the difference of one file between two commits (old, new)
    "file_diff": Op(("diff", "--no-color", "--no-renames", "-U<num>", *gitsafe._DIFF_FIXED, "--end-of-options",
                     "<commit>", "<commit>", "--", ":(literal)<file>")),
    # what a path is at a commit: `blob`, `tree`, ... (the caller needs `blob`; nothing is read)
    "blob_type": Op(("cat-file", "-t", "<commit>:./<file>")),
    # the files ONE commit changed under exactly this path (a folder would list its files; a file lists itself)
    "commit_file_names": Op(("show", *_SHOW, "--format=", "--name-status", "-z", *gitsafe._DIFF_FIXED,
                             "--end-of-options", "<commit>", "--", ":(literal)<file>")),
    # the same between two commits
    "range_file_names": Op(("diff", "--no-color", "--no-renames", "--name-status", "-z", *gitsafe._DIFF_FIXED,
                            "--end-of-options", "<commit>", "<commit>", "--", ":(literal)<file>")),
    # a file as it was at a commit: its size, then its bytes
    "blob_size": Op(("cat-file", "-s", "<commit>:./<file>")),
    "blob": Op(("cat-file", "blob", "<commit>:./<file>")),
    # a literal search of the INDEX (--cached: blobs from the object database, the working tree is not opened, so a
    # symlink there is never followed), no submodule: `path NUL line NUL text LF`
    "grep": Op(("grep", "--cached", "-I", "-n", "-z", "--no-color", "-F", "--no-recurse-submodules", "-e", "<pattern>",
                "--", ":(top)")),
    # a literal, case-insensitive search of EXACTLY the named files AS COMMITTED at a commit (not the index, not the
    # work tree): `<commit>^{commit}:path NUL line NUL text LF`; the caller strips the `<commit>^{commit}:` prefix. The
    # files are the caller's `files=` (each checked like <file> and given to git as a literal pathspec; none is
    # refused), so the caller bounds what is read by the list it passes (a folder name would match what is below it)
    "tree_grep": Op(("grep", "--no-color", "-I", "-n", "-z", "-i", "-F", "--no-recurse-submodules", "-e", "<pattern>",
                     "--end-of-options", "<commit>", "--", "<files>")),
    # the direct entries of ONE folder at a commit, nothing below it and no content: `<mode> SP <type> SP <id> SP
    # <size> TAB <name> NUL` per entry. A folder that is a link, a submodule or a file is not a tree: git refuses it.
    # Reads the object database only (the Wiki add-on lists its pages with the modes git recorded)
    "tree": Op(("ls-tree", "-z", "-l", "--end-of-options", "<commit>:./<file>")),
    # the commits that touched anything below ONE folder, newest first, each followed by the paths it changed there:
    # `NUL id NUL author NUL date NUL subject NUL LF path NUL path NUL ...` (see :func:`parse_dir_log`). A merge shows
    # no paths of its own (its parents' commits do)
    "dir_log": Op(("log", "--no-color", "--no-renames", f"--format={_COMMIT_RAW}", "--name-only", "-z",
                   "--max-count=<num>", "--end-of-options", "<commit>", "--", ":(literal)<file>")),
}  # fmt: skip
_NUM = re.compile(r"[0-9]{1,6}")
_BAD_CHAR = re.compile(r"[\x00-\x1f\x7f]")


def _check(kind: str, value: object) -> str:
    if not isinstance(value, str) or not value:
        raise Unsafe("invalid git operand")
    if kind == "commit":
        # a revision of gitsafe's closed forms (HEAD, a full id, refs/heads/<name>: no ':', '^', '..', '~'), and then
        # resolved AS A COMMIT: a tree id, a blob id or a tag is refused by git, so a diff between two blobs or a
        # `<tree>:<path>` lookup can never reach content that pathspecs and the path check do not govern
        return gitsafe._check("rev", value) + "^{commit}"
    if kind == "num":
        if not _NUM.fullmatch(value):
            raise Unsafe("invalid number")
        return value
    if kind == "pattern":
        if len(value) > 200 or _BAD_CHAR.search(value):
            raise Unsafe("invalid search text")
        return value
    parts = value.split("/")  # file
    if (
        len(value) > 4096
        or value.startswith("/")
        or any(p in ("", ".", "..") for p in parts)
        or _BAD_CHAR.search(value)
    ):
        raise Unsafe("invalid path")
    return value


def _fill(spec: Op, operand: tuple[str, ...], files: tuple[str, ...] = ()) -> list[str]:
    if len(operand) != len(spec.operands):
        raise Unsafe("wrong number of git operands")
    takes_files = "<files>" in spec.args
    if takes_files != bool(files) or len(files) > MAX_FILES:
        raise Unsafe("wrong number of files")
    it = iter(operand)
    out: list[str] = []
    for a in spec.args:
        if a == "<files>":
            out += [":(literal)" + _check("file", f) for f in files]
        else:
            out.append(_SLOT.sub(lambda m: _check(m.group(1), next(it)), a))
    return out


def command(
    repo: str,
    op: str,
    own: Mapping[str, str],
    *,
    operand: tuple[str, ...] = (),
    files: tuple[str, ...] = (),
    exclude_top: tuple[str, ...] = (),
    timeout: float = gitsafe.GATE_SECONDS,
) -> tuple[list[str], dict[str, str]]:
    """The argv and environment of operation ``op`` of :data:`OPS` against ``repo`` (absolute). Runs
    :func:`orch.gitsafe.gate` first. Raises :class:`orch.gitsafe.Unsafe` for an unknown operation, an invalid operand
    or a refusal of the gate (git must not run).

    **Race note (#565).** The gate checks the repository's config and the caller spawns git afterwards. Harmless here:
    every operation of :data:`OPS` is a history read (``log``, ``show`` and ``diff`` between commits, ``cat-file``,
    ``rev-parse``) or ``grep --cached``, which only reads the index: it opens no work-tree file, does not refresh the
    index and applies no filters. None touches the work tree, so no clean/smudge filter or ``fsmonitor`` can run;
    ``gitsafe._argv`` adds ``--no-ext-diff --no-textconv --no-pager`` and the diff pins, and git runs no hook for a
    read. A key a concurrent writer adds in the window therefore names no program these commands run."""
    spec = OPS.get(op)
    if spec is None:
        raise Unsafe("unknown git operation")
    args = _fill(spec, operand, files)
    if "--" in args:  # the deny list as pathspecs, and the caller's store names directly in the repository's top
        if any(not _TOP_NAME.fullmatch(n) for n in exclude_top):
            raise Unsafe("invalid exclusion")
        args += [*DENY_SPECS, *(f":(top,exclude,literal,icase){n}" for n in exclude_top)]
    local = gitsafe.gate(repo, own, timeout=timeout)
    entries = gitsafe._driver_pins(local) if spec.args[0] in ("log", "show", "diff") else []
    argv = gitsafe._argv(repo, args)
    argv[1:1] = _EXTRA
    env = gitsafe.isolated_env(own, entries)  # from scratch: no GIT_* variable of the caller reaches git
    if op != "layout":  # every other operation is pinned to the layout `layout` confirmed (see :func:`pins`)
        env.update(pins(repo))
    return argv, env


def pins(repo: str) -> dict[str, str]:
    """The directories git is told to use, instead of finding them: the real path of ``repo`` and its ``.git`` folder,
    exactly what the caller confirmed with the ``layout`` operation. With these set, a ``gitdir:`` redirect, a
    ``core.worktree``, a ``commondir`` or an alternate object directory of the repository's own files cannot move git
    elsewhere; ``GIT_ALTERNATE_OBJECT_DIRECTORIES`` and every other ``GIT_*`` variable of the caller are not passed."""
    real = os.path.realpath(repo)
    git_dir = os.path.join(real, ".git")
    return {
        "GIT_DIR": git_dir, "GIT_COMMON_DIR": git_dir, "GIT_WORK_TREE": real,
        "GIT_OBJECT_DIRECTORY": os.path.join(git_dir, "objects"),
    }  # fmt: skip


@dataclass(frozen=True)
class Result:
    code: int
    out: bytes
    cut: bool  # stopped because the output reached ``max_bytes`` or the time ran out
    timed_out: bool = False


def run(
    repo: str,
    op: str,
    own: Mapping[str, str],
    *,
    operand: tuple[str, ...] = (),
    files: tuple[str, ...] = (),
    exclude_top: tuple[str, ...] = (),
    max_bytes: int = 1 << 20,
    timeout: float = 15.0,
) -> Result:
    """Run one operation: at most ``max_bytes`` of output, at most ``timeout`` seconds (the whole gate included in the
    caller's budget is not counted: the gate has its own limit). The process group is killed when either is reached."""
    argv, env = command(repo, op, own, operand=operand, files=files, exclude_top=exclude_top)
    return spawn(argv, env, max_bytes=max_bytes, timeout=timeout)


def spawn(
    argv: list[str],
    env: Mapping[str, str],
    *,
    cwd: str = "/",
    cwd_fd: int | None = None,
    max_bytes: int = 1 << 20,
    timeout: float = 15.0,
) -> Result:
    """Run ``argv`` (built by this module, or a fixed search tool's) with stdin closed in its own process group:
    at most ``max_bytes`` of output and ``timeout`` seconds, then the group is killed. ``cwd_fd``: the child starts in
    the folder this descriptor holds (``fchdir`` before exec: no path is resolved again, so a folder swapped for a link
    after the caller checked it is not followed)."""
    try:
        proc = subprocess.Popen(  # noqa: S603 - the argv is a fixed template with validated operands
            argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, env=dict(env),
            cwd=None if cwd_fd is not None else cwd,
            preexec_fn=(lambda: os.fchdir(cwd_fd)) if cwd_fd is not None else None,  # noqa: PLW1509
            start_new_session=True, close_fds=True,
        )  # fmt: skip
    except OSError:
        raise Unsafe("the program cannot be started") from None
    assert proc.stdout is not None
    fd = proc.stdout.fileno()
    deadline = time.monotonic() + timeout
    buf = bytearray()
    cut = timed_out = False
    try:
        while True:
            left = deadline - time.monotonic()
            if left <= 0:
                cut = timed_out = True
                break
            if not fdwait.readable(fd, min(left, 0.5)):
                continue
            chunk = os.read(fd, 65536)
            if not chunk:
                break
            buf += chunk
            if len(buf) > max_bytes:
                cut = True
                del buf[max_bytes:]
                break
    finally:
        if cut or proc.poll() is None:
            with contextlib.suppress(OSError):
                os.killpg(proc.pid, signal.SIGKILL)  # the group this call created, nothing else
        with contextlib.suppress(Exception):
            proc.wait(timeout=5)
        proc.stdout.close()
    return Result(proc.returncode if proc.returncode is not None else -1, bytes(buf), cut, timed_out)


# --- parsers of the output (the formats above), total: a malformed record is dropped, never guessed ---------------

_SHA = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")


def _text(raw: bytes) -> str:
    return raw.decode("utf-8", "replace")


def parse_log(out: bytes) -> list[tuple[str, str, str, str]]:
    """``(sha, author, date, subject)`` per commit of ``file_log``, newest first."""
    pieces = out.split(b"\0")[1:]
    rows = []
    for i in range(0, len(pieces) - 3, 4):
        sha, who, when, subject = (_text(p) for p in pieces[i : i + 4])
        if _SHA.fullmatch(sha):
            rows.append((sha, who, when, subject.rstrip("\n")))
    return rows


_TREE_ROW = re.compile(r"([0-7]{6}) (blob|tree|commit) ([0-9a-f]{40}|[0-9a-f]{64}) +(-|[0-9]{1,12})\t([\s\S]*)")


def parse_tree(out: bytes) -> list[tuple[str, str, str, int | None, str]]:
    """``(mode, type, object id, size, name)`` per entry of ``tree`` (``ls-tree -z -l``); ``size`` is None for a tree or
    a submodule. A malformed record is dropped. ``name`` is one path component (a folder lists nothing deeper)."""
    rows = []
    chunks = out.split(b"\0")
    chunks.pop()  # after the last NUL: empty for a complete output, a cut-off record otherwise (never used)
    for chunk in chunks:
        m = _TREE_ROW.fullmatch(_text(chunk))
        if m:
            rows.append((m[1], m[2], m[3], None if m[4] == "-" else int(m[4]), m[5]))
    return rows


def parse_dir_log(out: bytes) -> list[tuple[str, str, str, str, list[str]]]:
    """``(sha, author, date, subject, [paths])`` per commit of ``dir_log``, newest first. Every path starts with the
    folder asked for, so a token that is exactly a commit id (after the empty token that starts a record) is a record
    start and never a path. A cut-off last name or header is dropped."""
    tokens = out.split(b"\0")
    tokens.pop()  # after the last NUL: empty for a complete output, a cut-off name otherwise (never used)
    rows: list[tuple[str, str, str, str, list[str]]] = []
    i, n = 0, len(tokens)
    while i < n:
        if i + 4 < n and not tokens[i] and _SHA.fullmatch(_text(tokens[i + 1])):
            sha, who, when, subject = (_text(t) for t in tokens[i + 1 : i + 5])
            i += 5
            paths: list[str] = []
            while i < n and not (not tokens[i] and i + 1 < n and _SHA.fullmatch(_text(tokens[i + 1]))):
                if tokens[i]:
                    paths.append(_text(tokens[i]).removeprefix("\n"))
                i += 1
            rows.append((sha, who, when, subject, paths))
        else:
            i += 1
    return rows


def parse_show(out: bytes) -> tuple[tuple[str, str, str, str, str], str] | None:
    """``((sha, author, date, subject, body), patch)`` of ``commit_file_patch``, or None if the record is malformed."""
    pieces = out.split(b"\0", 6)
    if len(pieces) != 7 or pieces[0] or not _SHA.fullmatch(_text(pieces[1])):
        return None
    sha, who, when, subject, body = (_text(p) for p in pieces[1:6])
    return (sha, who, when, subject, body.rstrip("\n")), _text(pieces[6]).lstrip("\n")


def parse_names(out: bytes) -> list[tuple[str, str]]:
    """``(status letter, path)`` per file of ``commit_names`` / ``range_names`` (``-z`` output)."""
    fields = out.split(b"\0")
    rows = []
    i = 0
    while i + 1 < len(fields):
        status = _text(fields[i]).strip()
        if status[:1].isalpha():
            rows.append((status[:1], _text(fields[i + 1])))
        i += 2
    return rows


def parse_grep(out: bytes) -> list[tuple[str, int, str]]:
    """``(path, line number, text)`` per hit of ``grep`` (``-n -z``: ``path NUL line NUL text LF``)."""
    rows = []
    for chunk in out.split(b"\n"):
        parts = chunk.split(b"\0", 2)
        if len(parts) == 3 and parts[1].strip().isdigit():
            rows.append((_text(parts[0]), int(parts[1]), _text(parts[2])))
    return rows
