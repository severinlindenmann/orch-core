"""A consistent copy of the hub state dir for a backup (protocol §17.6, Opus conditions 11 and the S0 carry-forward).

Plain files are read as they are. Every SQLite store is copied with the SQLite backup API (never its raw file, ``-wal`` or
``-shm``), then the rows that must not travel are deleted INSIDE the copy and the copy is vacuumed (so a deleted row's bytes
are not left in a free page): sessions (also switch-ticket sessions), pending TOTP enrolments, pairing codes, sign-in
challenges, failure/limiter rows, push tokens, known sources, EVERY unused invite (a reset or recovery link left in a
backup would be an account takeover), and the recovery notes. Paths the contract never lists (``setup-token``,
``route-queue.db``, logs, ``backups/`` …) are skipped by the contract's own rule."""

from __future__ import annotations

import hashlib
import io
import json
import os
import sqlite3
import stat
import tarfile
import tempfile
from pathlib import Path

from ..contract import backup as B

#: files the backup writes from the key material it is given, never from the disk
KEY_FILE = "backup-key.json"
GENERATED = frozenset({KEY_FILE})

#: ``store -> statements`` run on the COPY. A table that does not exist is skipped (older stores).
STRIP: dict[str, tuple[tuple[str, str], ...]] = {
    "channels.db": tuple(
        (t, f"DELETE FROM {t}")
        for t in ("sessions", "pairing", "failures", "challenges", "push_keys", "known_sources", "pending_totp")
    ),
    "invites.db": (("invites", "DELETE FROM invites WHERE used_ms IS NULL"),),
    "access.db": (("recovery_notes", "DELETE FROM recovery_notes"),),
}


class SnapshotError(Exception):
    """The state dir cannot be copied safely; ``code`` is stable and the text never carries file content."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _read_regular(path: str) -> bytes:
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise SnapshotError("not_regular")
        with os.fdopen(fd, "rb", closefd=False) as f:
            return f.read(B.MAX_TOTAL + 1)
    finally:
        os.close(fd)


def copy_db(path: Path, strip: tuple[tuple[str, str], ...] = ()) -> tuple[bytes, int]:
    """One store as a stripped, vacuumed, single-file copy, and the newest ``snapshots.version`` in it (0 when none)."""
    with tempfile.TemporaryDirectory(prefix="orchbak-") as td:  # 0700, removed with its content
        out = Path(td) / "copy.db"
        src = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True, timeout=15)
        try:
            dst = sqlite3.connect(out)
            try:
                src.backup(dst)
                dst.execute("PRAGMA secure_delete=ON")
                have = {r[0] for r in dst.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                newest = 0
                if "snapshots" in have:
                    row = dst.execute("SELECT MAX(version) FROM snapshots").fetchone()
                    newest = int(row[0] or 0)
                for table, sql in strip:
                    if table in have:
                        dst.execute(sql)
                dst.commit()
                dst.execute("VACUUM")
                dst.execute("PRAGMA journal_mode=DELETE")
            finally:
                dst.close()
        finally:
            src.close()
        for side in ("-wal", "-shm", "-journal"):
            if os.path.exists(str(out) + side):
                raise SnapshotError("sidecar")
        return _read_regular(str(out)), newest


def collect(state: Path) -> tuple[dict[str, bytes], dict[str, int]]:
    """Every file a backup lists: ``({path: bytes}, {"op_seq", "card_seq", "snapshot_seq"})``. ``meta.json`` is read first so
    the counters in the header and the ``op/<current>`` directory are the same generation."""
    try:
        meta = json.loads(_read_regular(str(state / "meta.json")))
        op_seq, card_seq = int(meta["op_seq"]), int(meta["card_seq"])
    except OSError, ValueError, KeyError, TypeError:
        raise SnapshotError("meta") from None
    files: dict[str, bytes] = {}
    total = 0
    snapshot_seq = 0
    stack: list[tuple[Path, str]] = [(state, "")]
    while stack:
        d, rel = stack.pop()
        with os.scandir(d) as it:
            entries = sorted(it, key=lambda e: e.name)
        for e in entries:
            path = f"{rel}{e.name}"
            if e.is_symlink():
                raise SnapshotError("symlink")
            if e.is_dir(follow_symlinks=False):
                if not B.valid_path(path) or B.is_excluded(path):
                    continue
                if rel == "op/" and e.name != str(op_seq):  # only the committed generation
                    continue
                stack.append((Path(e.path), path + "/"))
                continue
            if not e.is_file(follow_symlinks=False):
                continue  # a socket or other special file: never part of a backup
            if not B.valid_path(path) or B.is_excluded(path) or path in GENERATED:
                continue
            if path.endswith(".db"):
                data, newest = copy_db(Path(e.path), STRIP.get(path, ()) if "/" not in path else ())
                if path == "access.db":
                    snapshot_seq = newest
            else:
                data = _read_regular(e.path)
            total += len(data)
            if total > B.MAX_TOTAL or len(files) >= B.MAX_FILES - 2:
                raise SnapshotError("too_big")
            files[path] = data
    return files, {"op_seq": op_seq, "card_seq": card_seq, "snapshot_seq": snapshot_seq}


def file_list(files: dict[str, bytes]) -> list[dict[str, object]]:
    """The header's ``files`` entries, ascending by path (protocol §17.6)."""
    return [{"path": p, "size": len(files[p]), "sha256": hashlib.sha256(files[p]).hexdigest()} for p in sorted(files)]


def tar_files(files: dict[str, bytes]) -> bytes:
    """Regular files only, in path order, USTAR, zero times and owners, mode 0600 (a restore ignores them anyway)."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w", format=tarfile.USTAR_FORMAT) as tf:
        for p in sorted(files):
            ti = tarfile.TarInfo(p)
            ti.type, ti.mode, ti.mtime, ti.size = tarfile.REGTYPE, 0o600, 0, len(files[p])
            tf.addfile(ti, io.BytesIO(files[p]))
    return buf.getvalue()
