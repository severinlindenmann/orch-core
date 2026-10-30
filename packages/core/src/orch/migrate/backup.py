"""Backups before a migration, and restore.

A backup of one root goes to ``<data home>/orch/backups/<ts>-<kind>-<from>-<to>/`` (``manifest.json`` plus ``data/``):
mode 0700 under umask 077, **local only**, never inside a synced folder (iCloud Drive, Dropbox, Google Drive, OneDrive).
It holds the root's keys (they are as protected there as on disk); that is why it never leaves the machine.
The last 3 per root are kept. Left out: only derived or runtime data (the ``.state`` index and lock,
``.orch-work/``, ``install/tools``).

macOS on one volume: APFS clonefile (``cp -c``), instant and free of space. Otherwise a plain copy.

:func:`restore` puts a backup back and MERGES every ``revocations.jsonl`` (union of lines): it never takes a
revocation away.
"""

from __future__ import annotations

import json
import os
import secrets
import shutil
import subprocess
import sys
import time
from collections.abc import Mapping
from pathlib import Path

from orch import childenv, oslock
from orch.layout import STATE_DIR, WORK_DIR
from orch.ops.errors import OrchError

from .format import LOCK_FILE, lock_root
from .steps import atomic_write, is_derived

__all__ = ["KEEP", "backups_dir", "create_backup", "list_backups", "prune_backups", "restore"]

KEEP = 3
MANIFEST = "manifest.json"
DATA = "data"
#: top-level entries left out (derived or runtime), and one nested path
_SKIP_TOP = frozenset({WORK_DIR, LOCK_FILE})
_SKIP_REL = frozenset({"install/tools", "install/current", "install/update.json", "install/parts"})
_REVOCATIONS = "revocations.jsonl"
_SYNC_NAMES = frozenset(
    {"mobile documents", "dropbox", "google drive", "googledrive", "onedrive", "icloud drive", "clouddocs"}
)
_SYNC_PREFIXES = ("googledrive-", "dropbox-", "onedrive-", "icloud")
_SYNC_MARKERS = (".stfolder", ".dropbox", ".dropbox.cache", ".dropbox.attr", ".tmp.driveupload", ".tmp.drivedownload")


def backups_dir(env: Mapping[str, str] | None = None) -> Path:
    env = os.environ if env is None else env
    xdg = env.get("XDG_DATA_HOME", "")
    if xdg.startswith("/"):
        return Path(xdg) / "orch" / "backups"
    home = env.get("HOME") or str(Path.home())
    return Path(home) / ".local" / "share" / "orch" / "backups"


def synced_folder(path: Path, home: Path | None = None) -> Path | None:
    """The ancestor of ``path`` that a cloud client syncs (by name, location or marker file), or None."""
    p = Path(os.path.realpath(path))
    home_real = Path(os.path.realpath(home)) if home is not None else None
    for a in (p, *p.parents):
        name = a.name.lower()
        if name in _SYNC_NAMES or name.startswith(_SYNC_PREFIXES):
            return a
        if a.parent.name == "Library" and a.name in ("CloudStorage", "Mobile Documents"):
            return a
        if (
            sys.platform == "darwin"
            and home_real is not None
            and a.parent == home_real
            and a.name in ("Documents", "Desktop")
        ):
            return a  # iCloud "Desktop & Documents" syncs these when it is on
        if any((a / m).exists() for m in _SYNC_MARKERS):
            return a
    return None


def _exclude_from_time_machine(path: Path) -> None:
    """Best effort: keys must not leave this machine through Time Machine either."""
    if sys.platform != "darwin" or shutil.which("tmutil") is None:
        return
    try:
        subprocess.run(
            ["tmutil", "addexclusion", str(path)], capture_output=True, env=childenv.minimal(os.environ), timeout=20
        )
    except (OSError, subprocess.SubprocessError):
        pass


def _excluded(rel: str) -> bool:
    return rel.split("/")[0] in _SKIP_TOP or rel in _SKIP_REL or is_derived(rel)


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
        if entry.is_dir(follow_symlinks=False) and (
            erel in nested or erel == STATE_DIR or any(r.startswith(erel + "/") for r in _SKIP_REL)
        ):
            all_cloned = _copy(Path(entry.path), target, clone, erel) and all_cloned
            continue
        if clone and _clone(entry.path, target):
            continue
        all_cloned = False
        if entry.is_dir(follow_symlinks=False):
            shutil.copytree(entry.path, target, symlinks=True)
        else:
            shutil.copy2(entry.path, target, follow_symlinks=False)
    return all_cloned


def _clone(src: str, target: Path) -> bool:
    try:
        r = subprocess.run(["cp", "-c", "-R", src, str(target)], capture_output=True, env=childenv.minimal(os.environ))
    except OSError:
        return False
    if r.returncode == 0:
        return True
    if target.is_symlink() or target.is_file():
        target.unlink()
    elif target.exists():
        shutil.rmtree(target)  # a partial clone must not turn into a half-merged copy
    return False


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
) -> Path:
    """Back up ``root``. Returns the backup directory. Nothing is pruned here: :func:`prune_backups` runs only after
    the migration succeeded, so a failing update never eats its own way back."""
    root = Path(root).resolve()
    base = backups_dir(env)
    home = Path((os.environ if env is None else env).get("HOME") or Path.home())
    sync = synced_folder(base, home)
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
        _exclude_from_time_machine(base)
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
            _private(dest)
            manifest = {
                "v": 1, "kind": kind, "root": str(root), "from": from_n, "to": to_n,
                "method": "clone" if cloned else "copy", "created": time.time_ns(),
            }  # fmt: skip
            atomic_write(dest / MANIFEST, (json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode())
        except BaseException:
            shutil.rmtree(dest, ignore_errors=True)
            raise
    finally:
        os.umask(old)
    return dest


def _private(top: Path) -> None:
    """``chmod go=`` over the whole tree (a clone keeps the source modes; directories become 0700)."""
    for dirpath, dirnames, filenames in os.walk(top):
        for name in [*dirnames, *filenames]:
            p = os.path.join(dirpath, name)
            if os.path.islink(p):
                continue
            os.chmod(p, os.stat(p).st_mode & ~0o077)
        os.chmod(dirpath, os.stat(dirpath).st_mode & ~0o077)


def _manifest(d: Path) -> dict | None:
    try:
        m = json.loads((d / MANIFEST).read_text())
    except (OSError, ValueError):
        return None
    return m if isinstance(m, dict) else None


def list_backups(env: Mapping[str, str] | None, root: Path) -> list[Path]:
    """The backups of ``root``, newest first (by the creation time in the manifest, not the folder name)."""
    base, root = backups_dir(env), Path(root).resolve()
    found: list[tuple[int, str, Path]] = []
    if base.is_dir():
        for d in (p for p in base.iterdir() if p.is_dir()):
            m = _manifest(d)
            if m and m.get("root") == str(root):
                created = m.get("created")
                found.append((created if type(created) is int else 0, d.name, d))
    return [d for _, _, d in sorted(found, reverse=True)]


def prune_backups(
    env: Mapping[str, str] | None, root: Path, *, keep: int = KEEP, pin_from: int | None = None
) -> list[Path]:
    """Keep the newest ``keep`` backups of ``root`` plus the OLDEST backup made at format ``pin_from`` (the data as it
    was before the migration now pending). Returns what was removed."""
    backups = list_backups(env, root)
    pinned = None
    if pin_from is not None:
        pinned = next((d for d in reversed(backups) if (_manifest(d) or {}).get("from") == pin_from), None)
    removed = []
    for d in backups[keep:]:
        if d != pinned:
            shutil.rmtree(d, ignore_errors=True)
            removed.append(d)
    return removed


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


def _journal_allows(backup: Path, manifest: dict, env: Mapping[str, str] | None) -> bool:
    """The current, unfinished journal lists THIS backup (``backups``: {path: format it was taken at}) and the format
    matches the backup's ``from``: a backup left by an earlier update is never restored by a later one."""
    from .journal import read_journal

    try:
        doc = read_journal(env)
    except OrchError:
        return False
    if doc is None or doc["phase"] == "committed":
        return False
    listed = doc.get("backups")
    if not isinstance(listed, dict):
        return False
    want = listed.get(str(backup.resolve()))
    return type(want) is int and want == manifest.get("from")


def _exchange(a: Path, b: Path) -> bool:
    """Atomically swap two paths (macOS renamex_np RENAME_SWAP, Linux renameat2 RENAME_EXCHANGE). False if the system
    cannot: the caller then falls back to two renames."""
    try:
        import ctypes

        libc = ctypes.CDLL(None, use_errno=True)
        if sys.platform == "darwin":
            return libc.renamex_np(os.fsencode(a), os.fsencode(b), 2) == 0  # RENAME_SWAP
        if sys.platform.startswith("linux"):
            at_fdcwd = -100
            return libc.renameat2(at_fdcwd, os.fsencode(a), at_fdcwd, os.fsencode(b), 2) == 0  # RENAME_EXCHANGE
    except (OSError, AttributeError):
        pass
    return False


_SCRATCH = (".restore-", ".replaced-")


def _scratch_dirs(root: Path) -> list[Path]:
    return [p for p in root.parent.iterdir() if any(p.name.startswith(f".{root.name}{m}") for m in _SCRATCH)]


def _recover(root: Path) -> None:
    """A crash between the two renames of a swap leaves no root but a ``.replaced-`` folder: put it back. Leftover
    scratch folders of an earlier restore are removed (each by its exact path)."""
    scratch = _scratch_dirs(root) if root.parent.is_dir() else []
    if not root.exists():
        replaced = sorted(p for p in scratch if f".{root.name}.replaced-" in p.name)
        if replaced:
            os.rename(replaced[-1], root)
            scratch.remove(replaced[-1])
    for p in scratch:
        shutil.rmtree(p, ignore_errors=True)


def restore(
    backup: Path,
    root: Path,
    env: Mapping[str, str] | None = None,
    *,
    own_failure: bool = False,
    lock_fd: int | None = None,
) -> None:
    """Put ``backup`` back into ``root``. Allowed only before the commit point: the unfinished update journal must list
    this backup (``own_failure``: ``orch migrate``'s own failure path, no journal). Takes the exclusive lock (or uses
    ``lock_fd``, the caller's). The result is built in a staging folder next to the root: the backup, merged
    ``revocations.jsonl`` (union of lines), and everything the backup left out carried over from the current root. It is
    then swapped in atomically where the system can. The journal is marked first, so a crash never reopens the root."""
    from .journal import clear_journal, read_journal, write_journal

    backup, root = Path(backup), Path(root).resolve()
    manifest = _manifest(backup)
    src = backup / DATA
    if not manifest or manifest.get("v") != 1 or not src.is_dir():
        raise OrchError("migrate.backup_refused", f"{backup} is not a backup")
    if Path(manifest["root"]).resolve() != root:
        raise OrchError("migrate.backup_refused", "this backup was made of another data folder")
    for dirpath, dirnames, filenames in os.walk(src):
        if any(os.path.islink(os.path.join(dirpath, n)) for n in [*dirnames, *filenames]):
            raise OrchError("migrate.backup_refused", "the backup holds a link; it is not restored")
    if not own_failure and not _journal_allows(backup, manifest, env):
        raise OrchError(
            "migrate.backup_refused",
            "restore is only possible before an update is committed, and only for a backup this update made",
        )
    # a sidecar lock next to the root serialises restores (the root itself may be missing in a half-done swap)
    side = os.open(
        root.with_name(f".{root.name}.restore.lock"), os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600
    )
    try:
        oslock.flock(side, oslock.LOCK_EX | oslock.LOCK_NB)
    except BlockingIOError:
        os.close(side)
        raise OrchError("migrate.running", "a restore is already running") from None
    fd = -1
    try:
        if root.exists():  # the root lock first: a refused invocation must not touch the first run's leftovers
            fd = lock_root(root, exclusive=True) if lock_fd is None else lock_fd
        _recover(root)  # only under the locks (a missing root = a half-done swap, only the sidecar lock exists)
        if fd < 0:
            fd = lock_root(root, exclusive=True) if lock_fd is None else lock_fd
    except BaseException:
        if fd >= 0 and lock_fd is None:
            os.close(fd)
        os.close(side)
        raise
    staging = root.with_name(f".{root.name}.restore-{secrets.token_hex(4)}")
    aside = root.with_name(f".{root.name}.replaced-{secrets.token_hex(4)}")
    carried: list[str] = []  # entries moved into staging, so a failure can put them back
    created = read_journal(env) is None  # no journal yet: this restore makes one, and removes it again
    old = os.umask(0o077)
    try:
        write_journal(
            (read_journal(env) or {"phase": "stopped"})["phase"], env, restoring=str(root)
        )  # an unfinished journal from here on: a crash never reopens the root
        saved, current = _revocation_files(src), _revocation_files(root)
        shutil.copytree(src, staging, symlinks=True)
        for rel in set(saved) | set(current):
            have = saved.get(rel, [])
            merged = have + [x for x in current.get(rel, []) if x not in set(have)]
            atomic_write(staging / rel, ("\n".join(merged) + "\n").encode() if merged else b"")
        carry = [*sorted(_SKIP_TOP), *sorted(_SKIP_REL)]
        moved = carried
        st = root / STATE_DIR
        if st.is_dir():
            carry += [f"{STATE_DIR}/{n}" for n in sorted(os.listdir(st)) if is_derived(f"{STATE_DIR}/{n}")]
        for rel in carry:  # into staging BEFORE the swap; the journal file is copied, the original stays until the swap
            have = root / rel
            if not (have.exists() or have.is_symlink()):
                continue
            (staging / rel).parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            if rel == "install/update.json":
                shutil.copy2(have, staging / rel)
            else:
                os.rename(have, staging / rel)
                moved.append(rel)
        if _exchange(staging, root):  # now ``staging`` holds the old data
            shutil.rmtree(staging, ignore_errors=True)
        else:
            os.rename(root, aside)
            try:
                os.rename(staging, root)
            except BaseException:
                os.rename(aside, root)
                raise
            shutil.rmtree(aside, ignore_errors=True)
        doc = read_journal(env)
        if doc is not None:
            if created:
                clear_journal(env)
            else:
                write_journal(doc["phase"], env, restoring=None)
    finally:
        os.umask(old)
        if staging.exists():
            for rel in carried:  # a failed restore puts the carried entries back before staging is deleted
                if (staging / rel).exists() and root.exists() and not (root / rel).exists():
                    (root / rel).parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                    os.rename(staging / rel, root / rel)
            shutil.rmtree(staging, ignore_errors=True)
        if lock_fd is None:
            os.close(fd)
        os.close(side)
