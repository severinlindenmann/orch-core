"""The person-side records tool (H11e): commit exactly the host-named manifest, validate history, push fast-forward
only. Run as ``python -I -m orch.records`` (one shot, as the person; never the host, never the runner).

**Commit** (D-P2-33 items 1, 3, 4):

1. The repo is a dedicated records repo (:func:`orch.records.git.open_repo`), on a branch, with no merge, rebase or
   cherry-pick in progress, and a configuration that passed the deny-list.
2. The manifest comes from ``records.snapshot`` (all pages, one manifest id, recomputed here so pages of two cuts are
   never mixed; path rules and byte limits checked again).
3. Each file is opened beneath the record root component by component with ``O_NOFOLLOW``, exactly ``size`` bytes are
   read **once**, hashed, and those same bytes become the blob (``git fast-import``; the blob ids are computed here and
   checked against the repo). A mismatch takes a new snapshot once, then ``records.snapshot_changed``.
4. A temporary index is built **from empty** (``read-tree --empty``) and populated only from the manifest; the tree is
   written and ``ls-tree -r`` must equal the manifest exactly (paths, mode 100644, blob ids) before ``commit-tree``.
5. ``commit-tree`` (unsigned: F1's chain is the integrity, git is transport) with the documented trailer, then
   ``update-ref`` as a compare and swap on the branch.
6. The real index is never touched, except a compare-and-swap refresh: when it was exactly the old HEAD's tree, it is
   replaced (under ``index.lock``, only if its bytes are still the ones compared) by the new tree. Otherwise it is
   left alone and the result says ``index: "differs"``. The working tree is never written.

**History** (item 2): every commit reachable from the pushed commit and not from the last validated tip must be a
records commit of this workspace (the ``Orch-Records`` trailer, at most one parent) whose changes touch record paths
only, as regular files. Replace objects are off, grafts and shallow repos are refused. ``--fresh-history`` makes the
next commit a root commit instead (the old history stays in the repo but leaves the branch).

**Push** (items 6, D-P2-34): the source commit S is resolved once; the effective push URL is resolved once; the
history is validated; every blob reachable from S is scanned for an event with ``auth: "convenience"`` (#441); and
``records.export_check`` is asked with (S, URL, ref, that report), all **before any network call**. The check refuses
a non-private URL on the report alone, whatever the host's merged view says. A tip scanned clean is remembered
(``clean`` in the person's ``records.json``), so a later push scans only the new objects. Then the remote tip T comes
from ``ls-remote`` of that literal URL; T must exist locally and be an ancestor of S; and exactly S is pushed to that
URL with ``--force-with-lease=<ref>:<T>``. The lease only guards the race: it never authorizes a non-fast-forward,
because T is an ancestor of S. A missing remote branch needs ``--initial``.
"""

from __future__ import annotations

import contextlib
import hashlib
import os
import re
import stat
import tempfile
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import (
    FILE_MAX,
    FILES_MAX,
    PAGE_FILES,
    TOTAL_MAX,
    Entry,
    RecordsError,
    is_record_path,
    manifest_id,
    open_root,
    read_exact,
)
from .git import EMPTY_TREE, Git
from .person import PersonState

__all__ = [
    "AUTHOR",
    "Link",
    "Manifest",
    "ValidatedTips",
    "commit",
    "fetch_manifest",
    "history_has_convenience",
    "init_repo",
    "push",
    "validate_history",
]

Link = Callable[[str, dict[str, Any]], dict[str, Any]]
#: the records ops that are writes (an idem per call)
WRITE_OPS = frozenset({"records.remote_private", "records.note"})
AUTHOR = ("orch records", "records@orch.invalid")
TRAILER = "Orch-Records"
_FIXED_DATE = None  # commit dates are the clock's; tests may pin them through GIT_*_DATE in base_env


@dataclass(frozen=True)
class Manifest:
    id: str
    workspace: str
    ws_seq: int
    heads: dict[str, str | None]
    files: tuple[Entry, ...]


# --- the manifest ----------------------------------------------------------------------------------------------------


def fetch_manifest(link: Link, workspace_id: str) -> Manifest:
    """Every page of one ``records.snapshot``, checked: one manifest id, recomputed; record paths only, no
    duplicates; the byte and file limits; a bounded number of pages. A reply of the wrong shape is
    ``records.snapshot_invalid``."""
    try:
        return _fetch_manifest(link, workspace_id)
    except (KeyError, TypeError, ValueError, AttributeError):
        raise RecordsError("records.snapshot_invalid", "the host's snapshot reply has an unexpected shape") from None


def _fetch_manifest(link: Link, workspace_id: str) -> Manifest:
    data = link("records.snapshot", {})
    mid, heads, ws_seq = data["manifest"], data["heads"], data["ws_seq"]
    if type(mid) is not str or not isinstance(heads, dict) or type(ws_seq) is not int:
        raise RecordsError("records.snapshot_invalid", "the host's snapshot reply has an unexpected shape")
    if data.get("workspace") != workspace_id:
        raise RecordsError("records.snapshot_invalid", "the snapshot names another workspace")
    files = list(data["files"])
    pages = 1
    while data.get("next_page"):
        pages += 1
        if pages > FILES_MAX // PAGE_FILES + 2:
            raise RecordsError("records.snapshot_invalid", "the snapshot has more pages than files allow")
        data = link("records.snapshot", {"page": data["next_page"]})
        if data["manifest"] != mid:
            raise RecordsError("records.snapshot_invalid", "a snapshot page belongs to another manifest")
        files += list(data["files"])
        if len(files) > FILES_MAX:
            raise RecordsError("records.too_large", f"the manifest names more than {FILES_MAX} files")
    entries, seen, total = [], set(), 0
    for f in files:
        path, size, digest = f.get("path"), f.get("size"), f.get("sha256")
        if not is_record_path(path) or path in seen:
            raise RecordsError("records.snapshot_invalid", f"the manifest names {path!r}")
        if type(size) is not int or not 0 <= size <= FILE_MAX:
            raise RecordsError("records.too_large", f"{path}: {size} bytes")
        if type(digest) is not str or re.fullmatch(r"[0-9a-f]{64}", digest) is None:
            raise RecordsError("records.snapshot_invalid", f"{path}: no sha256")
        total += size
        if total > TOTAL_MAX:
            raise RecordsError("records.too_large", f"the records are over {TOTAL_MAX} bytes")
        seen.add(path)
        entries.append(Entry(path, size, digest))
    if manifest_id(workspace_id, ws_seq, heads, entries) != mid:
        raise RecordsError("records.snapshot_invalid", "the manifest id does not match its pages")
    return Manifest(mid, workspace_id, int(ws_seq), dict(heads), tuple(sorted(entries, key=lambda e: e.path)))


def _blob_id(data: bytes) -> str:
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()  # noqa: S324 - git's object id


def read_manifest_bytes(root: Path, manifest: Manifest) -> dict[str, bytes] | None:
    """The bytes of every manifest file, read once each; ``None`` when one no longer matches (replaced after the
    cut)."""
    root_fd = open_root(root)
    try:
        out: dict[str, bytes] = {}
        for e in manifest.files:
            data = read_exact(root_fd, e.path, e.size)
            if data is None or hashlib.sha256(data).hexdigest() != e.sha256:
                return None
            out[e.path] = data
        return out
    finally:
        os.close(root_fd)


def _store_blobs(repo: Git, blobs: dict[str, bytes]) -> dict[str, str]:
    """Write the blobs the repo lacks (one ``git fast-import``) and return path -> blob id, every id checked."""
    ids = {path: _blob_id(data) for path, data in blobs.items()}
    unique = sorted(set(ids.values()))
    present = _have(repo, unique)
    missing = [i for i in unique if i not in present]
    if missing:
        by_id = {ids[p]: blobs[p] for p in blobs}
        stream = b"".join(b"blob\ndata %d\n" % len(by_id[i]) + by_id[i] + b"\n" for i in missing)
        repo.run("fast-import", "--quiet", input=stream + b"done\n", timeout=60.0)  # type: ignore[arg-type]
        if _have(repo, missing) != set(missing):
            raise RecordsError("records.git_failed", "git did not store every blob")
    return ids


def stream_blobs(repo: Git, manifest: Manifest) -> dict[str, str] | None:
    """Read each manifest file once (exactly ``size`` bytes, beneath the record root fd, ``O_NOFOLLOW``), check its
    SHA-256 and stream those same bytes into one ``git fast-import`` as they are read: at most one file (64 MiB) is
    in memory, never the whole snapshot. ``None`` when a file no longer matches the manifest (replaced after the cut;
    the blobs already sent are unreferenced and harmless). Returns path -> blob id, every id checked in the repo.

    A watchdog kills ``fast-import`` when the run's budget is spent, so a blocked write cannot outlive it; its stderr
    goes nowhere (only the exit code is reported), so nothing can fill a pipe nobody reads."""
    import subprocess
    import threading

    from .git import HARDEN

    ids: dict[str, str] = {}
    sent: set[str] = set()
    proc = subprocess.Popen(
        ["git", *HARDEN, "fast-import", "--quiet"],
        stdin=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        env=repo.env(),
        cwd=repo.root,
    )
    watchdog = threading.Timer(repo.remaining(60.0), proc.kill)
    watchdog.daemon = True
    watchdog.start()
    ok = False
    try:
        root_fd = open_root(repo.root)
        try:
            for e in manifest.files:
                repo.remaining(1.0)  # the run's budget, between files
                data = read_exact(root_fd, e.path, e.size)
                if data is None or hashlib.sha256(data).hexdigest() != e.sha256:
                    return None
                oid = _blob_id(data)
                ids[e.path] = oid
                if oid not in sent:
                    assert proc.stdin is not None
                    proc.stdin.write(b"blob\ndata %d\n" % len(data) + data + b"\n")
                    sent.add(oid)
                del data
        finally:
            os.close(root_fd)
        assert proc.stdin is not None
        proc.stdin.write(b"done\n")
        proc.stdin.close()
        proc.wait()
        if proc.returncode != 0:
            if not watchdog.is_alive():
                raise RecordsError("records.budget", "storing the blobs used the run's budget; try again")
            raise RecordsError("records.git_failed", f"git fast-import failed ({proc.returncode})")
        ok = True
    except (BrokenPipeError, ValueError):
        if not watchdog.is_alive():
            raise RecordsError("records.budget", "storing the blobs used the run's budget; try again") from None
        raise RecordsError("records.git_failed", "git fast-import stopped early") from None
    finally:
        watchdog.cancel()
        if proc.stdin is not None and not proc.stdin.closed:
            try:
                proc.stdin.close()
            except BrokenPipeError:
                pass
        if not ok and proc.poll() is None:
            proc.kill()
        if proc.poll() is None or not ok:
            proc.wait()
    if _have(repo, sorted(sent)) != sent:
        raise RecordsError("records.git_failed", "git did not store every blob")
    return ids


def _have(repo: Git, ids: list[str]) -> set[str]:
    if not ids:
        return set()
    res = repo.run("cat-file", "--batch-check=%(objectname) %(objecttype)", input="\n".join(ids).encode() + b"\n")
    out = set()
    for line in res.stdout.decode().splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[1] == "blob":
            out.add(parts[0])
    return out


# --- the commit ----------------------------------------------------------------------------------------------------


@contextlib.contextmanager
def _temp_index() -> Iterator[Path]:
    with tempfile.TemporaryDirectory(prefix="orch-records-") as d:
        os.chmod(d, 0o700)
        yield Path(d) / "index"


def build_tree(repo: Git, manifest: Manifest, ids: Mapping[str, str]) -> str:
    """A tree of exactly the manifest: a temporary index from empty, the manifest entries, ``write-tree``, then
    ``ls-tree -r`` must equal the manifest (``records.tree_mismatch``)."""
    with _temp_index() as index:
        repo.run("read-tree", "--empty", index=index)
        info = b"".join(f"100644 {ids[e.path]}\t{e.path}".encode() + b"\0" for e in manifest.files)
        repo.run("update-index", "-z", "--index-info", input=info, index=index)
        tree = repo.out("write-tree", index=index)
    verify_tree(repo, tree, {e.path: ids[e.path] for e in manifest.files})
    return tree


def tree_entries(repo: Git, tree: str) -> dict[str, tuple[str, str, str]]:
    raw = repo.run("ls-tree", "-r", "-z", "--full-tree", tree).stdout
    out: dict[str, tuple[str, str, str]] = {}
    for item in raw.split(b"\0"):
        if not item:
            continue
        meta, _, path = item.partition(b"\t")
        mode, typ, oid = meta.decode().split(" ")
        out[path.decode(errors="surrogateescape")] = (mode, typ, oid)
    return out


def verify_tree(repo: Git, tree: str, want: Mapping[str, str]) -> None:
    have = tree_entries(repo, tree)
    expected = {p: ("100644", "blob", oid) for p, oid in want.items()}
    if have != expected:
        extra = sorted(set(have) - set(expected))[:3]
        missing = sorted(set(expected) - set(have))[:3]
        raise RecordsError(
            "records.tree_mismatch",
            f"the built tree is not exactly the manifest (extra {extra}, missing {missing}); nothing was committed",
        )


def _message(manifest: Manifest) -> bytes:
    return (
        f"orch: records ws_seq {manifest.ws_seq}\n\nOrch-Manifest: {manifest.id}\n{TRAILER}: {manifest.workspace}\n"
    ).encode()


_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_DIRECTORY = getattr(os, "O_DIRECTORY", 0)


@contextlib.contextmanager
def _git_dir_fd(repo: Git) -> Iterator[int]:
    """The git dir as a directory fd (no symlink): the real index and its lock are only reached beneath it."""
    try:
        fd = os.open(repo.git_dir, os.O_RDONLY | _DIRECTORY | _NOFOLLOW)
    except OSError:
        raise RecordsError("records.unsafe_tree", "the records repo's git dir cannot be opened safely") from None
    try:
        yield fd
    finally:
        os.close(fd)


def _write_new(path: Path, data: bytes) -> None:
    """A new file in a private temporary directory: created exclusively, never through a symlink."""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | _NOFOLLOW, 0o600)
    with os.fdopen(fd, "wb") as fh:
        fh.write(data)


def _index_tree(repo: Git, raw: bytes | None) -> str | None:
    """The tree a copy of the real index would write (the real index itself is never written), or None when it
    cannot (unmerged entries)."""
    if raw is None:
        return EMPTY_TREE
    with _temp_index() as index:
        _write_new(index, raw)
        res = repo.run("write-tree", index=index, check=False)
    return res.stdout.decode().strip() if res.returncode == 0 else None


def _read_at(dir_fd: int, name: str) -> bytes | None:
    try:
        fd = os.open(name, os.O_RDONLY | _NOFOLLOW, dir_fd=dir_fd)
    except FileNotFoundError:
        return None
    except OSError:
        raise RecordsError("records.unsafe_tree", f"the records repo's {name} is not a regular file") from None
    with os.fdopen(fd, "rb") as fh:
        if not stat.S_ISREG(os.fstat(fh.fileno()).st_mode):
            raise RecordsError("records.unsafe_tree", f"the records repo's {name} is not a regular file")
        return fh.read()


def _read_index(repo: Git) -> bytes | None:
    with _git_dir_fd(repo) as gfd:
        return _read_at(gfd, "index")


def _refresh_index(repo: Git, compared: bytes | None, new_commit: str) -> bool:
    """Compare and swap the real index to ``new_commit``'s tree: under ``index.lock`` (git's own lock, created
    exclusively beneath the pinned git dir fd), only if its bytes are still the ones compared."""
    with _temp_index() as index:
        repo.run("read-tree", new_commit, index=index)
        data = index.read_bytes()
    with _git_dir_fd(repo) as gfd:
        try:
            fd = os.open("index.lock", os.O_WRONLY | os.O_CREAT | os.O_EXCL | _NOFOLLOW, 0o644, dir_fd=gfd)
        except FileExistsError:
            return False
        done = False
        try:
            if _read_at(gfd, "index") != compared:
                return False
            view = memoryview(data)
            while view:
                view = view[os.write(fd, view) :]
            os.fsync(fd)
            os.close(fd)
            fd = -1
            os.replace("index.lock", "index", src_dir_fd=gfd, dst_dir_fd=gfd)
            done = True
            return True
        finally:
            if fd >= 0:
                os.close(fd)
            if not done:
                with contextlib.suppress(FileNotFoundError):
                    os.unlink("index.lock", dir_fd=gfd)


def commit(repo: Git, link: Link, workspace_id: str, *, fresh: bool = False) -> dict[str, Any]:
    """Commit exactly the host-named manifest on the current branch. ``fresh``: a root commit (fresh history)."""
    branch = repo.branch()
    ref = f"refs/heads/{branch}"
    ids: dict[str, str] | None = None
    manifest: Manifest | None = None
    for _attempt in range(2):
        manifest = fetch_manifest(link, workspace_id)
        ids = stream_blobs(repo, manifest)
        if ids is not None:
            break
    if ids is None or manifest is None:
        raise RecordsError("records.snapshot_changed", "a record changed after the cut twice; try again")
    tree = build_tree(repo, manifest, ids)
    old = repo.resolve(ref)
    old_tree = repo.out("rev-parse", f"{old}^{{tree}}") if old else EMPTY_TREE
    if old is not None and old_tree == tree and not fresh:
        return {"commit": old, "tree": tree, "manifest": manifest.id, "ws_seq": manifest.ws_seq, "changed": False,
                "branch": branch, "index": "unchanged", "fresh": False}  # fmt: skip
    args = ["commit-tree", "--no-gpg-sign", tree]
    if old is not None and not fresh:
        args += ["-p", old]
    real_before = _read_index(repo)
    index_matches = _index_tree(repo, real_before) == old_tree
    name, email = AUTHOR
    repo.base_env.update(
        GIT_AUTHOR_NAME=name, GIT_AUTHOR_EMAIL=email, GIT_COMMITTER_NAME=name, GIT_COMMITTER_EMAIL=email
    )
    new = repo.out(*args, "-F", "-", input=_message(manifest))  # type: ignore[arg-type]
    repo.run("update-ref", "-m", "orch records", ref, new, old or "0" * 40)
    index = "refreshed" if index_matches and _refresh_index(repo, real_before, new) else "differs"
    return {"commit": new, "tree": tree, "manifest": manifest.id, "ws_seq": manifest.ws_seq, "changed": True,
            "branch": branch, "index": index, "fresh": fresh}  # fmt: skip


# --- history ---------------------------------------------------------------------------------------------------------


def _commits(repo: Git, tip: str, since: str | None) -> list[str]:
    args = ["rev-list", tip] + (["--not", since] if since else [])
    return repo.out(*args).split()


_ALLOWED_HEADERS = ("tree", "parent", "author", "committer")


def _cat_commits(repo: Git, ids: list[str]) -> dict[str, tuple[list[str], bytes]]:
    """id -> (header names in order, raw message)."""
    raw = repo.run("cat-file", "--batch", input="\n".join(ids).encode() + b"\n").stdout
    out: dict[str, tuple[list[str], bytes]] = {}
    pos = 0
    for oid in ids:
        nl = raw.index(b"\n", pos)
        header = raw[pos:nl].decode().split()
        if len(header) != 3 or header[1] != "commit":
            raise RecordsError("records.history_invalid", f"{oid} is not a commit")
        size = int(header[2])
        body = raw[nl + 1 : nl + 1 + size]
        pos = nl + 1 + size + 1
        head, _, message = body.partition(b"\n\n")
        names = [line.split(b" ", 1)[0].decode(errors="replace") for line in head.split(b"\n")]
        out[oid] = (names, message)
    return out


def _message_ok(message: bytes, workspace_id: str) -> bool:
    """Exactly the shape :func:`_message` writes, nothing else exported in a message."""
    pattern = (
        rb"orch: records ws_seq (?:0|[1-9][0-9]{0,15})\n\nOrch-Manifest: m_[0-9a-f]{64}\n"
        + TRAILER.encode()
        + rb": "
        + re.escape(workspace_id.encode())
        + rb"\n"
    )
    return re.fullmatch(pattern, message) is not None


def _headers_ok(names: list[str]) -> bool:
    """Only tree, parent, author and committer headers, in git's order (no gpgsig, mergetag, encoding, extras)."""
    if any(n not in _ALLOWED_HEADERS for n in names):
        return False
    return names[:1] == ["tree"] and names[-2:] == ["author", "committer"] and names.count("author") == 1


def validate_history(repo: Git, workspace_id: str, tip: str, since: str | None = None) -> int:
    """Every commit reachable from ``tip`` (and not from ``since``, a tip validated before) is a records commit of
    this workspace whose changes touch record paths only, as regular files (``records.history_invalid``,
    ``records.not_records_only``). Returns how many commits were checked."""
    repo.no_grafts()
    if since is not None and not _is_ancestor(repo, since, tip):
        since = None  # the validated tip left the history: check all of it
    ids = _commits(repo, tip, since)
    if not ids:
        return 0
    meta = _cat_commits(repo, ids)
    for oid in ids:
        names, message = meta[oid]
        if names.count("parent") > 1:
            raise RecordsError("records.history_invalid", f"{oid[:12]} is a merge: records history is linear")
        if not _headers_ok(names):
            raise RecordsError(
                "records.not_records_only",
                f"{oid[:12]} has commit headers a records commit never has (signature, encoding, extras)",
                hint="push by hand, or start a fresh history: orch records commit --fresh-history",
            )
        if not _message_ok(message, workspace_id):
            raise RecordsError(
                "records.not_records_only",
                f"{oid[:12]} is not a records commit of this workspace",
                hint="push by hand, or start a fresh history: orch records commit --fresh-history",
            )
    raw = repo.run("diff-tree", "--stdin", "-r", "-z", "--no-renames", "--root", input="\n".join(ids).encode() + b"\n")
    tokens = raw.stdout.split(b"\0")
    current = None
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        if tok.startswith(b":"):
            fields = tok[1:].decode().split(" ")
            path = tokens[i + 1].decode(errors="surrogateescape")
            i += 2
            new_mode, status = fields[1], fields[4]
            if status == "D":
                continue
            if not is_record_path(path) or new_mode != "100644":
                raise RecordsError(
                    "records.history_invalid",
                    f"{(current or '?')[:12]} holds {path!r} (mode {new_mode}), which is not a record: the history "
                    "cannot be pushed",
                    hint="start a fresh history: orch records commit --fresh-history",
                )
            continue
        if tok:
            current = tok.decode()
        i += 1
    return len(ids)


#: the scanner's rules; a clean-tip cache entry of another version is ignored (a full rescan)
SCANNER_VERSION = 2
#: the event logs: the only record files whose lines are events (bodies and artifacts are free text agents write)
EVENT_LOG = re.compile(r"events/workspace\.jsonl|tickets/[0-7][0-9A-HJKMNP-TV-Z]{25}/events\.jsonl")
#: bytes of blob content read per ``cat-file --batch`` call
_SCAN_CHUNK = 64 * 1024 * 1024


def _blob_has_convenience(data: bytes, seen: set[bytes] | None = None) -> bool:
    """A line of an event-log blob is an event with ``auth: "convenience"``
    (:func:`orch.ops.readers.line_is_convenience`: lenient JSON; a line that does not parse counts, fail closed).
    ``seen`` holds lines already judged clean in this scan (logs are append-only: each version repeats the older
    lines)."""
    from orch.ops.readers import line_is_convenience

    for line in data.split(b"\n"):
        if seen is not None:
            if line in seen:
                continue
        if line_is_convenience(line):
            return True
        if seen is not None:
            seen.add(line)
    return False


def _event_log_blobs(repo: Git, tip: str, since: str | None) -> list[str]:
    """Every blob that sits at an event-log path in a commit reachable from ``tip`` and not from ``since``: what each
    of those commits added or changed there (the history is linear, validated before). Blobs at an event-log path in
    ``since``'s history were scanned clean under the same rules (:data:`SCANNER_VERSION`)."""
    ids = _commits(repo, tip, since)
    if not ids:
        return []
    raw = repo.run("diff-tree", "--stdin", "-r", "-z", "--no-renames", "--root", input="\n".join(ids).encode() + b"\n")
    tokens = raw.stdout.split(b"\0")
    out: dict[str, None] = {}
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        if tok.startswith(b":"):
            fields = tok[1:].decode().split(" ")
            path = tokens[i + 1].decode(errors="surrogateescape")
            i += 2
            if fields[4] != "D" and EVENT_LOG.fullmatch(path):
                out[fields[3]] = None
            continue
        i += 1
    return list(out)


def history_has_convenience(repo: Git, tip: str, since: str | None = None) -> bool:
    """Whether an event log in the history a push exports (reachable from ``tip``, and not from ``since``, a tip
    scanned clean before) holds an event with ``auth: "convenience"`` (#441), whatever the host's merged view says
    now. Only event-log paths count. Any git error raises (the push fails closed)."""
    repo.no_grafts()
    if since is not None and not _is_ancestor(repo, since, tip):
        since = None
    blobs_ids = _event_log_blobs(repo, tip, since)
    if not blobs_ids:
        return False
    fmt = "--batch-check=%(objectname) %(objecttype) %(objectsize)"
    lines = repo.run("cat-file", fmt, input="\n".join(blobs_ids).encode() + b"\n").stdout.decode().splitlines()
    if len(lines) != len(blobs_ids):
        raise RecordsError("records.history_invalid", "git listed objects it cannot describe")
    blobs: list[tuple[str, int]] = []
    for line in lines:
        parts = line.split()
        if len(parts) != 3 or parts[1] != "blob" or not parts[2].isdigit():
            raise RecordsError("records.history_invalid", "an event log of the history is missing or not a file")
        blobs.append((parts[0], int(parts[2])))
    seen: set[bytes] = set()
    chunk: list[tuple[str, int]] = []
    size = 0
    for item in [*blobs, None]:
        if item is not None and (not chunk or size + item[1] <= _SCAN_CHUNK):
            chunk.append(item)
            size += item[1]
            continue
        if chunk and _chunk_has_convenience(repo, chunk, seen):
            return True
        if item is not None:
            chunk, size = [item], item[1]
    return False


def _chunk_has_convenience(repo: Git, chunk: list[tuple[str, int]], seen: set[bytes]) -> bool:
    raw = repo.run("cat-file", "--batch", input="\n".join(oid for oid, _ in chunk).encode() + b"\n").stdout
    pos = 0
    for oid, size in chunk:
        nl = raw.index(b"\n", pos)
        header = raw[pos:nl].split()
        if len(header) != 3 or header[0].decode() != oid or int(header[2]) != size:
            raise RecordsError("records.history_invalid", f"{oid[:12]} could not be read")
        data = raw[nl + 1 : nl + 1 + size]
        pos = nl + 1 + size + 1
        if _blob_has_convenience(data, seen):
            return True
    return False


def _is_ancestor(repo: Git, a: str, b: str) -> bool:
    return repo.run("merge-base", "--is-ancestor", a, b, check=False).returncode == 0


# --- validated tips (the person's records.json) -------------------------------------------------------------------


ValidatedTips = PersonState  # the validated tips live in the person's records.json (orch.records.person)


# --- push ------------------------------------------------------------------------------------------------------------


def push_url(repo: Git, remote: str, net: Git | None = None) -> str:
    """The one effective push URL of ``remote`` as git resolves it under the frozen configuration (``pushurl``, else
    ``url``; ``insteadOf``/``pushInsteadOf`` applied). Not exactly one URL, or a transport outside the allow-list, is
    refused."""
    from .git import check_local, redact, url_allowed, url_problem

    check_local(repo.local, remote)
    res = repo.run("remote", "get-url", "--push", "--all", remote, check=False)
    if res.returncode != 0:
        raise RecordsError("records.remote_missing", f"the records repo has no remote {remote!r}")
    urls = [u for u in res.stdout.decode().splitlines() if u]
    if len(urls) != 1:
        n = len(urls)
        raise RecordsError("records.remote_invalid", f"remote {remote!r} has {n} push URLs; exactly one is needed")
    url = urls[0]
    problem = url_problem(url)
    if problem is not None:
        raise RecordsError("records.remote_invalid", f"the push URL of remote {remote!r} is refused: {problem}")
    if not url_allowed(url, repo.protocols):
        raise RecordsError("records.remote_invalid", f"remote {remote!r}: transport not allowed ({redact(url[:80])})")
    # the literal URL as git itself resolves it in the transport repo (no url.*, no remote.*): the same string
    if net is None:
        with repo.transport() as fresh:
            seen = fresh.out("ls-remote", "--get-url", url)
    else:
        seen = net.out("ls-remote", "--get-url", url)
    if seen != url:
        raise RecordsError("records.remote_invalid", "git would rewrite the push URL again; refusing")
    return url


def remote_tip(net: Git, url: str, ref: str) -> str | None:
    """T from ``ls-remote`` of the literal URL, run in the transport repo."""
    res = net.run("ls-remote", "--refs", url, ref, network=True)
    tips = [line.split("\t") for line in res.stdout.decode().splitlines() if "\t" in line]
    exact = [sha for sha, name in tips if name == ref]
    if len(exact) > 1:
        raise RecordsError("records.push_rejected", f"the remote lists {ref} twice")
    return exact[0] if exact else None


def push(
    repo: Git,
    link: Link,
    workspace_id: str,
    tips: ValidatedTips,
    *,
    remote: str = "origin",
    initial: bool = False,
    expect_url: str | None = None,
) -> dict[str, Any]:
    """Push exactly the branch's current commit, fast-forward only, after the history and the export check.
    ``expect_url`` (#1198 C15): the address the person reviewed; the push URL this run resolves and uses must be exactly
    that, or nothing is sent (``records.remote_invalid``). One resolution: the one the push goes to."""
    branch = repo.branch()
    ref = f"refs/heads/{branch}"
    source = repo.resolve(ref)  # S, resolved once
    if source is None:
        raise RecordsError("records.git_state", f"{branch} has no commit yet: orch records commit first")
    with repo.transport() as net:
        return _push(repo, net, link, workspace_id, tips, remote, initial, branch, ref, source, expect_url)


def _push(
    repo: Git, net: Git, link: Link, workspace_id: str, tips: ValidatedTips, remote: str, initial: bool,
    branch: str, ref: str, source: str, expect_url: str | None = None,
) -> dict[str, Any]:  # fmt: skip
    url = push_url(repo, remote, net)
    if expect_url is not None and url != expect_url:
        raise RecordsError(
            "records.remote_invalid",
            "the remote's address is not the one you reviewed, so nothing was changed. Look at it again.",
        )
    repo.no_grafts()
    # before any network call: the history, then the export check with the scan of what the push exports (#441)
    checked = validate_history(repo, workspace_id, source, tips.get(workspace_id, branch))
    tips.set(workspace_id, branch, source)
    conv = history_has_convenience(repo, source, tips.clean(workspace_id, branch, SCANNER_VERSION))
    check = link("records.export_check", {"sha": source, "url": url, "ref": ref, "history_convenience": conv})
    if check.get("sha") != source or check.get("url") != url or check.get("ref") != ref:
        raise RecordsError("records.push_rejected", "the export check answered for another push")
    if not conv:
        tips.set_clean(workspace_id, branch, source, SCANNER_VERSION)
    target = remote_tip(net, url, ref)  # T
    if target is None and not initial:
        raise RecordsError(
            "records.push_rejected",
            f"the remote has no {ref}",
            hint="a first push creates it: orch records push --initial (terminal)",
        )
    if target is not None:
        if repo.run("cat-file", "-e", f"{target}^{{commit}}", check=False).returncode != 0:
            raise RecordsError(
                "records.push_rejected",
                f"the remote's {ref} ({target[:12]}) is not in the records repo: it diverged; resolve it by hand",
            )
        if target != source and not _is_ancestor(repo, target, source):
            raise RecordsError(
                "records.push_rejected",
                f"the remote's {ref} ({target[:12]}) is not an ancestor of {source[:12]}: never a non-fast-forward",
            )
    if target == source:
        return {"pushed": False, "commit": source, "url": url, "ref": ref, "reason": "up_to_date"}
    repo.recheck_local()
    if push_url(repo, remote, net) != url:  # the destination is the one checked, right before the push
        raise RecordsError("records.push_rejected", "the push destination changed during this run")
    lease = f"--force-with-lease={ref}:{target or ''}"
    res = net.run(
        "-c", "push.followTags=false", "-c", "push.gpgSign=false", "-c", "push.recurseSubmodules=no",
        "push", "--no-verify", "--porcelain", "--no-recurse-submodules", "--no-follow-tags", lease,
        url, f"{source}:{ref}",
        network=True, check=False,
    )  # fmt: skip
    if res.returncode != 0:
        from .git import redact

        why = redact((res.stderr.decode(errors="replace").strip().splitlines() or ["?"])[-1][:300])
        raise RecordsError("records.push_rejected", f"the remote refused the push (no rebase, no force): {why}")
    return {"pushed": True, "commit": source, "from": target, "url": url, "ref": ref, "checked": checked,
            "policy": check.get("policy"), "check": check.get("check")}  # fmt: skip


# --- init ------------------------------------------------------------------------------------------------------------


def init_repo(root: Path, *, branch: str = "main", env: Mapping[str, str] | None = None) -> dict[str, Any]:
    """A dedicated records repo at the record root: ``git init`` when there is none, and ``.state/`` (and every
    non-record name) kept out of ``git status`` through ``info/exclude`` (no tracked ``.gitignore``: the tree holds
    records only)."""
    import subprocess

    from .git import LOCAL_TIMEOUT, _base_env, pinned_root

    root = pinned_root(root)
    created = False
    if not os.path.lexists(root / ".git"):
        subprocess.run(
            ["git", "-c", "core.hooksPath=/dev/null", "init", "-q", "--template=", "-b", branch, str(root)],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            # no template and the system and global config off (#541): no hooks, no template config, no init.templateDir
            env={
                **_base_env(os.environ if env is None else env, "file"),
                "GIT_CONFIG_NOSYSTEM": "1",
                "GIT_CONFIG_GLOBAL": os.devnull,
            },
            timeout=LOCAL_TIMEOUT,
            check=True,
        )
        created = True
    from .git import git_dir_of

    git_dir = git_dir_of(root)
    lines = ["# orch records: only the store's record files are tracked", ".state/", "addons/"]
    gfd = os.open(git_dir, os.O_RDONLY | _DIRECTORY | _NOFOLLOW)
    try:
        with contextlib.suppress(FileExistsError):
            os.mkdir("info", 0o755, dir_fd=gfd)
        ifd = os.open("info", os.O_RDONLY | _DIRECTORY | _NOFOLLOW, dir_fd=gfd)
        try:
            raw = _read_at(ifd, "exclude")
            have = (raw or b"").decode(errors="replace").splitlines()
            if not all(line in have for line in lines):
                data = ("\n".join(have + [line for line in lines if line not in have]) + "\n").encode()
                tmp = f"exclude.orch.{os.getpid()}"
                fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | _NOFOLLOW, 0o644, dir_fd=ifd)
                with os.fdopen(fd, "wb") as fh:
                    fh.write(data)
                os.replace(tmp, "exclude", src_dir_fd=ifd, dst_dir_fd=ifd)
        finally:
            os.close(ifd)
    finally:
        os.close(gfd)
    return {"created": created}
