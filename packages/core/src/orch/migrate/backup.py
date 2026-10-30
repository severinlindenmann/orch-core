"""Backups before a migration, and restore.

A backup of one root goes to ``<data home>/orch/backups/<ts>-<kind>-<from>-<to>/`` (``manifest.json`` plus ``data/``):
mode 0700 under umask 077, **local only**, never inside a synced folder (iCloud Drive, Dropbox, Google Drive, OneDrive).
It holds the root's keys (they are as protected there as on disk); that is why it never leaves the machine.
The last 3 per root are kept. Derived data (``.state/``, ``.orch-work/``, ``install/tools``) is left out.

macOS on one volume: APFS clonefile (``cp -c``), instant and free of space. Otherwise a plain copy.

:func:`restore` puts a backup back and MERGES every ``revocations.jsonl`` (union of lines): it never takes a
revocation away.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from collections.abc import Mapping
from pathlib import Path

from orch.ops.errors import OrchError

from .format import LOCK_FILE, lock_root
from .steps import atomic_write

__all__ = ["KEEP", "backups_dir", "create_backup", "list_backups", "restore"]

KEEP = 3
MANIFEST = "manifest.json"
DATA = "data"
#: top-level entries left out (derived or runtime), and one nested path
_SKIP_TOP = frozenset({".state", ".orch-work", LOCK_FILE})
_SKIP_REL = frozenset({"install/tools"})
_REVOCATIONS = "revocations.jsonl"
_SYNC_NAMES = frozenset(
    {"mobile documents", "dropbox", "google drive", "googledrive", "onedrive", "icloud drive", "clouddocs"}
)
_SYNC_PREFIXES = ("googledrive-", "dropbox-", "onedrive-", "icloud")
_SYNC_MARKERS = (".dropbox", ".dropbox.cache", ".dropbox.attr", ".tmp.driveupload", ".tmp.drivedownload")


def backups_dir(env: Mapping[str, str] | None = None) -> Path:
    env = os.environ if env is None else env
    xdg = env.get("XDG_DATA_HOME", "")
    if xdg.startswith("/"):
        return Path(xdg) / "orch" / "backups"
    home = env.get("HOME") or str(Path.home())
    return Path(home) / ".local" / "share" / "orch" / "backups"


def synced_folder(path: Path) -> Path | None:
    """The ancestor of ``path`` that a cloud client syncs (by its name or its marker files), or None."""
    p = Path(os.path.realpath(path))
    for a in (p, *p.parents):
        name = a.name.lower()
        if name in _SYNC_NAMES or name.startswith(_SYNC_PREFIXES):
            return a
        if any((a / m).exists() for m in _SYNC_MARKERS):
            return a
    return None


def _excluded(rel: str) -> bool:
    return rel.split("/")[0] in _SKIP_TOP or rel in _SKIP_REL


def _copy(src: Path, dst: Path, clone: bool, rel: str = "") -> bool:
    """Copy ``src`` (a directory) into ``dst`` without the excluded entries. Returns True if every entry was cloned."""
    dst.mkdir(mode=0o700, exist_ok=True)
    all_cloned = clone
    nested = {r.split("/")[0] for r in _SKIP_REL if "/" in r}
    for entry in sorted(os.scandir(src), key=lambda e: e.name):
        erel = f"{rel}/{entry.name}" if rel else entry.name
        if _excluded(erel):
            continue
        target = dst / entry.name
        if entry.is_dir(follow_symlinks=False) and (erel in nested or any(r.startswith(erel + "/") for r in _SKIP_REL)):
            all_cloned = _copy(Path(entry.path), target, clone, erel) and all_cloned
            continue
        if clone and subprocess.run(["cp", "-c", "-R", entry.path, str(target)], capture_output=True).returncode == 0:
            continue
        all_cloned = False
        if entry.is_dir(follow_symlinks=False):
            shutil.copytree(entry.path, target, symlinks=True)
        else:
            shutil.copy2(entry.path, target, follow_symlinks=False)
    return all_cloned


def _same_volume(a: Path, b: Path) -> bool:
    return a.stat().st_dev == b.stat().st_dev


def create_backup(
    root: Path,
    kind: str,
    from_n: int,
    to_n: int,
    env: Mapping[str, str] | None = None,
    *,
    ts: str | None = None,
    keep: int = KEEP,
) -> Path:
    """Back up ``root`` and prune to the last ``keep`` backups of this root. Returns the backup directory."""
    root = Path(root).resolve()
    base = backups_dir(env)
    sync = synced_folder(base)
    if sync is not None:
        raise OrchError(
            "migrate.backup_refused",
            f"the backup folder {base} is inside a synced folder ({sync}): backups hold keys and stay on this machine",
        )
    if root == base or root in base.parents or base in root.parents:
        raise OrchError("migrate.backup_refused", "the backup folder and the data folder must be separate")
    old = os.umask(0o077)
    try:
        base.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(base, 0o700)
        stamp = ts or time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
        name = f"{stamp}-{kind}-{from_n}-{to_n}"
        dest, n = base / name, 1
        while True:
            try:
                dest.mkdir(mode=0o700)
                break
            except FileExistsError:
                n += 1
                dest = base / f"{name}-{n}"
        try:
            clone = sys.platform == "darwin" and _same_volume(root, base)
            cloned = _copy(root, dest / DATA, clone)
            manifest = {
                "v": 1, "kind": kind, "root": str(root), "from": from_n, "to": to_n,
                "method": "clone" if cloned else "copy",
            }  # fmt: skip
            atomic_write(dest / MANIFEST, (json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode())
            os.chmod(dest / DATA, 0o700)
        except BaseException:
            shutil.rmtree(dest, ignore_errors=True)
            raise
    finally:
        os.umask(old)
    for old_backup in list_backups(env, root)[keep:]:
        shutil.rmtree(old_backup, ignore_errors=True)
    return dest


def list_backups(env: Mapping[str, str] | None, root: Path) -> list[Path]:
    """The backups of ``root``, newest first."""
    base, root = backups_dir(env), Path(root).resolve()
    out: list[Path] = []
    if not base.is_dir():
        return out
    for d in sorted((p for p in base.iterdir() if p.is_dir()), key=lambda p: p.name, reverse=True):
        try:
            m = json.loads((d / MANIFEST).read_text())
        except (OSError, ValueError):
            continue
        if isinstance(m, dict) and m.get("root") == str(root):
            out.append(d)
    return out


def _lines(path: Path) -> list[str]:
    try:
        return path.read_text().splitlines()
    except FileNotFoundError:
        return []


def _revocation_files(top: Path) -> dict[str, list[str]]:
    found: dict[str, list[str]] = {}
    for dirpath, dirnames, filenames in os.walk(top):
        rel_dir = Path(dirpath).relative_to(top)
        if rel_dir.parts[:1] and _excluded(rel_dir.parts[0]):
            dirnames.clear()
            continue
        if _REVOCATIONS in filenames:
            found[(rel_dir / _REVOCATIONS).as_posix()] = _lines(Path(dirpath) / _REVOCATIONS)
    return found


def restore(backup: Path, root: Path) -> None:
    """Put ``backup`` back into ``root`` (exclusive lock: refused while orch runs). Every ``revocations.jsonl`` is
    the union of the backup's lines and the current ones, never replaced."""
    backup, root = Path(backup), Path(root)
    try:
        manifest = json.loads((backup / MANIFEST).read_text())
        src = backup / DATA
        ok = manifest.get("v") == 1 and src.is_dir()
    except (OSError, ValueError, AttributeError):
        ok = False
    if not ok:
        raise OrchError("migrate.backup_refused", f"{backup} is not a backup")
    if Path(manifest["root"]).resolve() != root.resolve():
        raise OrchError("migrate.backup_refused", "this backup was made of another data folder")
    fd = lock_root(root, exclusive=True)
    try:
        current = _revocation_files(root)
        for entry in os.scandir(root):
            if entry.name in _SKIP_TOP:
                continue
            if entry.is_dir(follow_symlinks=False):
                shutil.rmtree(entry.path)
            else:
                os.unlink(entry.path)
        old = os.umask(0o077)
        try:
            _restore_tree(src, root)
        finally:
            os.umask(old)
        saved = _revocation_files(src)
        for rel in set(saved) | set(current):
            have = saved.get(rel, [])
            merged = have + [x for x in current.get(rel, []) if x not in set(have)]
            atomic_write(root / rel, ("\n".join(merged) + "\n").encode() if merged else b"")
    finally:
        os.close(fd)


def _restore_tree(src: Path, dst: Path) -> None:
    for entry in os.scandir(src):
        target = dst / entry.name
        if entry.is_dir(follow_symlinks=False):
            shutil.copytree(entry.path, target, symlinks=True, dirs_exist_ok=True)
        else:
            shutil.copy2(entry.path, target, follow_symlinks=False)
