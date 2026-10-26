"""Records commit and push (H11e, addon #308; orch-v2-host D-P2-33/34 as ruled by the 12 Oct tie-break).

The **records** of a workspace are the store's own files: ``config.json``, ``keys.jsonl``, ``AGENTS.orch.md``,
``events/workspace.jsonl`` and, per ticket, ``ticket.json``, ``body.md``, ``events.jsonl`` and ``artifacts/*``. Never
``.state/``, never ``addons/``, never anything else under the root. Git adds history, backup and transport, not trust.

**Verification scope.** Git metadata (the ``Orch-Records`` trailer, author, committer, the manifest line) is transport
metadata, never authorization or freshness: a consumer verifies a clone by F1 replay and checkpoints like any copy,
and ``AGENTS.orch.md`` and ``config.json`` get no authority from sitting in the repo.

This package holds what the host op (:mod:`orch.host.records`) and the person-side tool (``python -I -m
orch.records``, :mod:`orch.records.tool`) share: the record path rules, the limits, the manifest id and the safe,
single-read file reader. Pure Python, no git here.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import time
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

__all__ = [
    "FILES_MAX",
    "FILE_MAX",
    "PAGE_FILES",
    "RECORD_PATH",
    "TOTAL_MAX",
    "TTL_SECONDS",
    "Entry",
    "RecordsError",
    "is_record_path",
    "list_records",
    "manifest_id",
    "read_exact",
]

#: one file at most (``records.too_large``)
FILE_MAX = 64 * 1024 * 1024
#: one snapshot at most, all files together
TOTAL_MAX = 1024 * 1024 * 1024
#: files in one snapshot at most (bounds the host's work under the lock and the tool's memory)
FILES_MAX = 200_000
#: files per ``records.snapshot`` page
PAGE_FILES = 2000
#: one run of the tool, git and host calls together (D-P2-33: 60 s per run)
RUN_BUDGET = 60.0
#: the clock of the run budget (a seam: tests replace it to show that a human confirmation restarts the budget)
MONOTONIC = time.monotonic
#: a manifest is served this long after it was taken
TTL_SECONDS = 600

_ULID = r"[0-7][0-9A-HJKMNP-TV-Z]{25}"
_ART = r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}"
#: exactly the record paths (F1 store layout); everything else is never committed
RECORD_PATH = re.compile(
    rf"(?:config\.json|keys\.jsonl|AGENTS\.orch\.md|events/workspace\.jsonl"
    rf"|tickets/{_ULID}/(?:ticket\.json|body\.md|events\.jsonl|artifacts/{_ART}))"
)
_TOP = ("config.json", "keys.jsonl", "AGENTS.orch.md")
_TICKET_FILES = ("ticket.json", "body.md", "events.jsonl")
_ULID_RE = re.compile(_ULID)
_ART_RE = re.compile(_ART)
_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_DIRECTORY = getattr(os, "O_DIRECTORY", 0)
_CLOEXEC = getattr(os, "O_CLOEXEC", 0)


class RecordsError(Exception):
    """A refusal of the records tool (or of a records host op, which maps it to a host error with the same code and
    exit): ``code`` is one of :attr:`EXITS` (the records codes; a few callers pass a host code such as
    ``op.not_allowed`` with an explicit exit), ``exit`` the CLI exit code. Integrity and policy refusals exit 3,
    ``records.snapshot_invalid`` (a malformed host reply) 5, a retryable ``records.snapshot_changed`` 9."""

    EXITS = {
        "records.not_dedicated": 3,
        "records.git_state": 3,
        "records.unsafe_config": 3,
        "records.not_records_only": 3,
        "records.history_invalid": 3,
        "records.snapshot_changed": 9,
        "records.snapshot_invalid": 5,
        "records.tree_mismatch": 3,
        "records.too_large": 3,
        "records.unsafe_tree": 3,
        "records.push_rejected": 3,
        "records.remote_invalid": 3,
        "records.remote_missing": 3,
        "records.git_failed": 3,
        "records.unsupported": 3,
        "records.busy": 9,
        "records.budget": 9,
        "records.unconfirmed": 3,
        "records.invalid": 5,
        "records.timer_failed": 1,
        "records.timer_conflict": 3,
    }

    def __init__(self, code: str, message: str, *, hint: str | None = None, exit_: int | None = None) -> None:
        super().__init__(f"{code}: {message}")
        self.code, self.message, self.hint = code, message[:400], hint
        self.exit = exit_ if exit_ is not None else self.EXITS.get(code, 1)


def is_record_path(path: object) -> bool:
    return type(path) is str and RECORD_PATH.fullmatch(path) is not None


@dataclass(frozen=True)
class Entry:
    """One file of a manifest: its record path, byte size and SHA-256 (hex)."""

    path: str
    size: int
    sha256: str

    def as_dict(self) -> dict[str, Any]:
        return {"path": self.path, "size": self.size, "sha256": self.sha256}


def manifest_id(workspace_id: str, ws_seq: int, heads: Mapping[str, str | None], files: Iterable[Entry]) -> str:
    """The manifest id: SHA-256 over (workspace id, ws_seq, chain heads, file list), canonical JSON. The tool
    recomputes it from the pages it got, so pages of two cuts can never be mixed."""
    body = {
        "v": 1,
        "workspace": workspace_id,
        "ws_seq": int(ws_seq),
        "heads": {k: heads[k] for k in sorted(heads)},
        "files": [[e.path, e.size, e.sha256] for e in sorted(files, key=lambda e: e.path)],
    }
    raw = json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()
    return "m_" + hashlib.sha256(raw).hexdigest()


# --- safe reading under a pinned root ------------------------------------------------------------------------------


def _open_dir(parent_fd: int, name: str) -> int:
    fd = os.open(name, os.O_RDONLY | _DIRECTORY | _NOFOLLOW | _CLOEXEC, dir_fd=parent_fd)
    if not stat.S_ISDIR(os.fstat(fd).st_mode):  # pragma: no cover - O_DIRECTORY already refuses
        os.close(fd)
        raise NotADirectoryError(name)
    return fd


def open_record(root_fd: int, rel: str) -> int:
    """Open the record ``rel`` beneath ``root_fd`` one component at a time with ``O_NOFOLLOW``: a symlinked ancestor or
    a non-regular file is refused (``records.unsafe_tree``). The caller closes the fd."""
    if not is_record_path(rel):
        raise RecordsError("records.unsafe_tree", f"{rel!r} is not a record path")
    parts = rel.split("/")
    fds: list[int] = []
    try:
        cur = root_fd
        for part in parts[:-1]:
            cur = _open_dir(cur, part)
            fds.append(cur)
        fd = os.open(parts[-1], os.O_RDONLY | _NOFOLLOW | _CLOEXEC, dir_fd=cur)
    except OSError as exc:
        raise RecordsError(
            "records.unsafe_tree", f"{rel}: cannot open safely ({exc.strerror or type(exc).__name__})"
        ) from None
    finally:
        for d in fds:
            os.close(d)
    if not stat.S_ISREG(os.fstat(fd).st_mode):
        os.close(fd)
        raise RecordsError("records.unsafe_tree", f"{rel} is not a regular file")
    return fd


def read_exact(root_fd: int, rel: str, size: int) -> bytes | None:
    """Exactly ``size`` bytes of the record ``rel`` (a log may have grown since the cut: only its committed prefix is
    read), read **once**. ``None`` when the file is now shorter than ``size`` (replaced after the cut)."""
    if size > FILE_MAX:
        raise RecordsError("records.too_large", f"{rel} is {size} bytes; the limit is {FILE_MAX}")
    fd = open_record(root_fd, rel)
    try:
        chunks, left = [], size
        while left > 0:
            b = os.read(fd, min(left, 1 << 20))
            if not b:
                return None
            chunks.append(b)
            left -= len(b)
        return b"".join(chunks)
    finally:
        os.close(fd)


def open_root(root: Path) -> int:
    """The record root as a directory fd (the root itself must not be a symlink)."""
    try:
        return os.open(root, os.O_RDONLY | _DIRECTORY | _NOFOLLOW | _CLOEXEC)
    except OSError as exc:
        raise RecordsError(
            "records.unsafe_tree", f"cannot open the record root ({exc.strerror or type(exc).__name__})"
        ) from None


def list_records(root_fd: int) -> list[str]:
    """Every record path present under the root, sorted. Names that are not record names are ignored (they are never
    committed); a record name that is a symlink, a directory where a file belongs or a symlinked ticket directory is
    ``records.unsafe_tree``."""
    out: list[str] = []

    def lexists(dir_fd: int, name: str) -> os.stat_result | None:
        try:
            return os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
        except FileNotFoundError:
            return None

    def regular(dir_fd: int, name: str, rel: str) -> bool:
        st = lexists(dir_fd, name)
        if st is None:
            return False
        if not stat.S_ISREG(st.st_mode):
            raise RecordsError("records.unsafe_tree", f"{rel} is not a regular file")
        return True

    for name in _TOP:
        if regular(root_fd, name, name):
            out.append(name)
    for sub in ("events", "tickets"):
        st = lexists(root_fd, sub)
        if st is not None and not stat.S_ISDIR(st.st_mode):
            raise RecordsError("records.unsafe_tree", f"{sub} is not a directory")
    try:
        events = _open_dir(root_fd, "events")
    except FileNotFoundError:
        events = None
    except OSError as exc:
        raise RecordsError("records.unsafe_tree", f"events: {exc.strerror or type(exc).__name__}") from None
    if events is not None:
        try:
            if regular(events, "workspace.jsonl", "events/workspace.jsonl"):
                out.append("events/workspace.jsonl")
        finally:
            os.close(events)
    try:
        tickets = _open_dir(root_fd, "tickets")
    except FileNotFoundError:
        return sorted(out)
    except OSError as exc:
        raise RecordsError("records.unsafe_tree", f"tickets: {exc.strerror or type(exc).__name__}") from None
    try:
        for uid in sorted(os.listdir(tickets)):
            if not _ULID_RE.fullmatch(uid):
                continue
            st = lexists(tickets, uid)
            if st is None:
                continue
            if not stat.S_ISDIR(st.st_mode):
                raise RecordsError("records.unsafe_tree", f"tickets/{uid} is not a directory")
            tfd = _open_dir(tickets, uid)
            try:
                if len(out) > FILES_MAX:
                    raise RecordsError("records.too_large", f"more than {FILES_MAX} record files")
                for name in _TICKET_FILES:
                    if regular(tfd, name, f"tickets/{uid}/{name}"):
                        out.append(f"tickets/{uid}/{name}")
                ast = lexists(tfd, "artifacts")
                if ast is None:
                    continue
                if not stat.S_ISDIR(ast.st_mode):
                    raise RecordsError("records.unsafe_tree", f"tickets/{uid}/artifacts is not a directory")
                afd = _open_dir(tfd, "artifacts")
                try:
                    for art in sorted(os.listdir(afd)):
                        if _ART_RE.fullmatch(art) and regular(afd, art, f"tickets/{uid}/artifacts/{art}"):
                            out.append(f"tickets/{uid}/artifacts/{art}")
                            if len(out) > FILES_MAX:
                                raise RecordsError("records.too_large", f"more than {FILES_MAX} record files")
                finally:
                    os.close(afd)
            finally:
                os.close(tfd)
    finally:
        os.close(tickets)
    return sorted(out)


def hash_records(root_fd: int, paths: list[str], cache: dict[tuple[Any, ...], str] | None = None) -> list[Entry]:
    """Size and SHA-256 of each record, enforcing :data:`FILE_MAX` and :data:`TOTAL_MAX` (``records.too_large``) before
    reading. ``cache`` maps (path, size, ino, mtime_ns, ctime_ns) to a digest, so immutable artifacts are not re-hashed
    on every cut while the store lock is held."""
    entries: list[Entry] = []
    total = 0
    for rel in paths:
        fd = open_record(root_fd, rel)
        try:
            st = os.fstat(fd)
            size = st.st_size
            if size > FILE_MAX:
                raise RecordsError("records.too_large", f"{rel} is {size} bytes; the limit is {FILE_MAX}")
            total += size
            if total > TOTAL_MAX:
                raise RecordsError("records.too_large", f"the records are over {TOTAL_MAX} bytes")
            key = (rel, size, st.st_ino, st.st_mtime_ns, st.st_ctime_ns)
            digest = cache.get(key) if cache is not None else None
            if digest is None:
                h, left = hashlib.sha256(), size
                while left > 0:
                    b = os.read(fd, min(left, 1 << 20))
                    if not b:
                        raise RecordsError("records.snapshot_changed", f"{rel} shrank while it was read")
                    h.update(b)
                    left -= len(b)
                digest = h.hexdigest()
                if cache is not None:
                    cache[key] = digest
        finally:
            os.close(fd)
        entries.append(Entry(rel, size, digest))
    return entries
