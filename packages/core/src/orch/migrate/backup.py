"""Backups before a migration, and restore.

INVARIANT (keep it when you change this file): everything a restore needs in order to recover from a crash lives OUTSIDE
the root being restored: the lock and the record of the staging and aside paths are in a sidecar folder next to the root
(``<parent>/.<rootname>.restore/``, 0700, ours). Recovery never depends on the update journal, which for the host root
sits inside the folder that gets renamed. Recovery NEVER deletes anything while the root is missing or the record is
missing or unreadable: it refuses in plain words and leaves everything in place. Nothing creates a path inside a
missing root. Deleting is allowed only when the root is present and the record says the path is ours.

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

import errno
import json
import os
import re
import secrets
import shutil
import stat
import subprocess
import sys
import time
from collections.abc import Callable, Mapping
from pathlib import Path

from orch import childenv, oslock, privdir
from orch.layout import STATE_DIR, WORK_DIR
from orch.ops.errors import OrchError

from .format import LOCK_FILE, lock_root
from .journal import restore_pending as journal_restore_pending
from .steps import NESTED_PART_ROOTS, PART_RUNTIME, atomic_write, is_derived

__all__ = ["KEEP", "backups_dir", "create_backup", "list_backups", "prune_backups", "restore"]

KEEP = 3
MANIFEST = "manifest.json"
DATA = "data"
#: top-level entries left out (derived or runtime), and one nested path
_SKIP_TOP = frozenset({WORK_DIR, LOCK_FILE})
#: NESTED_PART_ROOTS: the part data dirs are roots of their own (backed up and restored by themselves). A restore of
#: the host root carries the CURRENT ones over (``_carry_list``): it never replaces, merges or reads them. Order of a
#: rollback (``orch.update.engine``): the part roots first, then the host, so the host's swap cannot undo a part.
#: PART_RUNTIME (``install/parts/<part>/pid``) is runtime like them: left out of the backup, current one carried over.
_SKIP_REL = frozenset({"install/tools", "install/current", "install/update.json", *NESTED_PART_ROOTS, *PART_RUNTIME})
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
        if rel_dir.parts and _excluded(rel_dir.as_posix()):  # also nested ones: a part root's own revocations
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


NONCE_FILE = ".orch-restore-nonce"


def _move(src: Path, dst: Path) -> None:
    try:
        os.rename(src, dst)
    except OSError as exc:
        if exc.errno == errno.EXDEV:
            raise OrchError(
                "migrate.backup_refused",
                f"{src.name} is on another disk than the data folder and cannot be moved safely; nothing was lost",
            ) from None
        raise


def _carry_list(base: Path) -> list[str]:
    """The entries a restore carries from the current root (everything the backup leaves out), as found in ``base``."""
    carry = [*sorted(_SKIP_TOP), *sorted(_SKIP_REL)]
    st = base / STATE_DIR
    if st.is_dir():
        carry += [f"{STATE_DIR}/{n}" for n in sorted(os.listdir(st)) if is_derived(f"{STATE_DIR}/{n}")]
    return carry


def _put_back(staging: Path, root: Path) -> None:
    """Move every carried entry the root is missing back out of ``staging``."""
    for rel in _carry_list(staging):
        if (staging / rel).exists() and not (root / rel).exists() and not (root / rel).is_symlink():
            (root / rel).parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            _move(staging / rel, root / rel)


def _trusted(p: Path) -> bool:
    """Ours, not a link, not writable by anyone else (checked with lstat: nothing is followed). The write rule is
    orch.privdir's one answer for person-owned paths."""
    try:
        st = os.lstat(p)
    except OSError:
        return False
    uid = os.getuid()
    return stat.S_ISDIR(st.st_mode) and st.st_uid == uid and not privdir.others_can_write(st, uid)


def _remove(p: Path) -> None:
    """Delete one scratch entry by its exact path; a link is unlinked, never followed."""
    if p.is_symlink() or not p.is_dir():
        try:
            p.unlink()
        except OSError:
            pass
    else:
        shutil.rmtree(p, ignore_errors=True)


_RECORD_NAME = re.compile(r"\.restore-[0-9a-f]{8}|\.replaced-[0-9a-f]{8}")


def _say_stderr(msg: str) -> None:
    print(msg, file=sys.stderr)


class _Sidecar:
    """``<parent>/.<rootname>.restore/`` (0700, ours, lstat-checked): the restore lock and the record of the staging
    and aside paths. It lives OUTSIDE the root being restored, so a rename of the root never takes it along."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.dir = root.with_name(f".{root.name}.restore")
        self.record = self.dir / "record.json"
        self._fd = -1

    def open(self) -> None:
        if not self.root.parent.is_dir():
            raise OrchError("migrate.backup_refused", "the folder around the data folder is missing")
        if os.path.lexists(self.dir):
            st = os.lstat(self.dir)
            if not stat.S_ISDIR(st.st_mode) or st.st_uid != os.getuid() or st.st_mode & 0o077:
                raise OrchError("migrate.backup_refused", f"{self.dir.name} is not a folder of ours; it is not used")
        else:
            os.mkdir(self.dir, 0o700)
        flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
        try:
            fd = os.open(self.dir / "lock", flags, 0o600)
        except OSError as exc:
            raise OrchError(
                "migrate.backup_refused",
                f"the restore lock in {self.dir.name} cannot be used ({exc.strerror or exc}); it may be a link",
            ) from None
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_uid != os.getuid():
            os.close(fd)
            raise OrchError("migrate.backup_refused", "the restore lock is not a file of ours")
        try:
            oslock.flock(fd, oslock.LOCK_EX | oslock.LOCK_NB)
        except BlockingIOError:
            os.close(fd)
            raise OrchError("migrate.running", "a restore is already running") from None
        self._fd = fd

    def read(self) -> dict | None:
        """The record, None if there is none; an unreadable or foreign one raises (nothing is deleted then)."""
        try:
            with open(self.record, "rb") as f:
                doc = json.loads(f.read(1 << 16))
        except FileNotFoundError:
            return None
        except (OSError, ValueError):
            doc = None
        ok = isinstance(doc, dict) and doc.get("v") == 1 and doc.get("root") == str(self.root)
        paths = [doc.get(k) for k in ("staging", "aside")] if ok else []
        for q in paths:
            ok = ok and isinstance(q, str) and Path(q).parent == self.root.parent
            ok = ok and _RECORD_NAME.fullmatch(Path(q).name.removeprefix(f".{self.root.name}")) is not None
        if not ok:
            raise OrchError(
                "migrate.backup_refused",
                f"the restore record in {self.dir.name} is unreadable; nothing was touched. Look at that folder",
            )
        return doc

    def write(self, **fields: str) -> None:
        atomic_write(self.record, (json.dumps({"v": 1, "root": str(self.root), **fields}) + "\n").encode())

    def finish(self) -> None:
        for name in ("record.json", "lock"):
            try:
                os.unlink(self.dir / name)
            except OSError:
                pass
        try:
            os.rmdir(self.dir)
        except OSError:
            pass

    def close(self) -> None:
        if self._fd >= 0:
            os.close(self._fd)
            self._fd = -1


def _leftovers(root: Path, recorded: set[Path], say: Callable[[str], None]) -> None:
    """Name, once each, the scratch-like folders beside the root that no record owns. They are left alone."""
    seen: set[str] = set()
    for p in sorted(root.parent.iterdir()):
        n = p.name
        if p in recorded or n in seen or not n.startswith((f".{root.name}.restore-", f".{root.name}.replaced-")):
            continue
        seen.add(n)
        say(f"found a leftover folder orch did not record: {ascii(n)}; check and remove it yourself")


def _has_nonce(root: Path, nonce: object) -> bool:
    try:
        return isinstance(nonce, str) and (root / NONCE_FILE).read_text().strip() == nonce
    except OSError:
        return False


def _recover(root: Path, side: _Sidecar, say: Callable[[str], None]) -> None:
    """Under the sidecar lock, before anything else. Acts ONLY on the paths in the sidecar record, each checked with
    lstat (ours, a real folder, not group/world writable). It never deletes while it is unsure: no root, or a record it
    cannot read, means it refuses in plain words and leaves everything where it is.

    * root missing: the recorded ``aside`` is the old root, renamed away by a killed swap: put it back, then the
      carried entries held in the recorded staging folder; without a recorded aside it refuses.
    * root present: the recorded staging holds carried entries not yet in the root (put them back first), or the
      old data of a finished swap; the recorded staging and aside are then deleted. Scratch folders NOT in the
      record are ignored: never imported, never deleted."""
    rec = side.read()
    recorded = {Path(rec[k]) for k in ("staging", "aside")} if rec else set()
    if root.exists():
        _leftovers(root, recorded, say)
    if rec is None:
        if not root.exists():
            raise OrchError(
                "migrate.backup_refused",
                f"{root.name} is missing and there is no record of a restore: nothing was touched",
            )
        return
    staging, aside = Path(rec["staging"]), Path(rec["aside"])
    present = [q for q in (staging, aside) if os.path.lexists(q)]
    for q in present:
        if not _trusted(q):
            raise OrchError("migrate.backup_refused", f"{q.name} is not a folder of ours; it is not used")
    if not root.exists():
        if aside not in present:
            raise OrchError(
                "migrate.backup_refused",
                f"{root.name} is missing and the folder that held it is gone too: nothing was deleted. "
                f"Look at {staging.name}",
            )
        _move(aside, root)
        present.remove(aside)
    elif aside in present and not _has_nonce(root, rec.get("nonce")):
        # a root that is not the staged one (re-created after a kill) stands where the old root was: the aside is the
        # real old data. Delete nothing, say where everything is.
        raise OrchError(
            "migrate.backup_refused",
            f"{root.name} is not the folder the restore built, and {aside.name} holds your previous data "
            f"(carried folders may be in {staging.name}): nothing was deleted. Check them and rename the right one to "
            f"{root.name}",
        )
    if staging in present:
        _put_back(staging, root)
    for q in present:
        _remove(q)
    for f in (root / NONCE_FILE, side.record):
        try:
            os.unlink(f)
        except OSError:
            pass


def restore(
    backup: Path,
    root: Path,
    env: Mapping[str, str] | None = None,
    *,
    own_failure: bool = False,
    lock_fd: int | None = None,
    say: Callable[[str], None] | None = None,
) -> None:
    """Put ``backup`` back into ``root``. Allowed only before the commit point: the unfinished update journal must list
    this backup (``own_failure``: ``orch migrate``'s own failure path, no journal). Takes the root's exclusive lock (or
    uses ``lock_fd``, the caller's). The result is built in a staging folder next to the root: the backup, merged
    ``revocations.jsonl`` (union of lines), and everything the backup left out carried over from the current root. It is
    then swapped in atomically where the system can. The journal is marked first, so a crash never reopens the root,
    and the sidecar record is written before the first move, so a crash can always be put right."""

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
    side = _Sidecar(root)
    side.open()
    fd = -1
    try:
        # The journal check comes after recovery when the root is missing (a host root keeps its journal inside it).
        _recover(root, side, say or _say_stderr)
        if not own_failure and not _journal_allows(backup, manifest, env):
            raise OrchError(
                "migrate.backup_refused",
                "restore is only possible before an update is committed, and only for a backup this update made",
            )
        fd = lock_root(root, exclusive=True) if lock_fd is None else lock_fd
        _swap_in(backup, src, root, side, env, own_failure)
        side.finish()
    finally:
        if fd >= 0 and lock_fd is None:
            os.close(fd)
        side.close()


def _swap_in(
    backup: Path, src: Path, root: Path, side: _Sidecar, env: Mapping[str, str] | None, own_failure: bool
) -> None:
    from .journal import clear_journal, read_journal, write_journal

    staging = root.with_name(f".{root.name}.restore-{secrets.token_hex(4)}")
    aside = root.with_name(f".{root.name}.replaced-{secrets.token_hex(4)}")
    carried: list[str] = []
    created = read_journal(env) is None  # no journal yet: this restore makes one, and removes it again
    old = os.umask(0o077)
    try:
        nonce = secrets.token_hex(16)
        side.write(staging=str(staging), aside=str(aside), backup=str(backup), nonce=nonce)  # before the first move
        write_journal((read_journal(env) or {"phase": "stopped"})["phase"], env, restoring=str(root))
        saved, current = _revocation_files(src), _revocation_files(root)
        shutil.copytree(src, staging, symlinks=True)
        atomic_write(staging / NONCE_FILE, nonce.encode())  # proves, after the swap, that this root is the staged one
        for rel in set(saved) | set(current):
            have = saved.get(rel, [])
            merged = have + [x for x in current.get(rel, []) if x not in set(have)]
            atomic_write(staging / rel, ("\n".join(merged) + "\n").encode() if merged else b"")
        # into staging BEFORE the swap; the journal file is copied, the original stays until the swap
        for rel in _carry_list(root):
            have = root / rel
            if not (have.exists() or have.is_symlink()):
                continue
            (staging / rel).parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            if rel == "install/update.json":
                shutil.copy2(have, staging / rel)
            else:
                _move(have, staging / rel)
                carried.append(rel)
        if _exchange(staging, root):  # now ``staging`` holds the old data
            shutil.rmtree(staging, ignore_errors=True)
        else:
            _move(root, aside)
            try:
                _move(staging, root)
            except BaseException:
                _move(aside, root)
                raise
            shutil.rmtree(aside, ignore_errors=True)
        try:
            os.unlink(root / NONCE_FILE)
        except OSError:
            pass
        doc = read_journal(env)
        if doc is not None:
            if created:
                clear_journal(env)
            else:
                write_journal(doc["phase"], env, restoring=None)
    finally:
        os.umask(old)
        if staging.exists() and root.exists():  # never delete staging while the root is missing: it may hold data
            _put_back(staging, root)
            shutil.rmtree(staging, ignore_errors=True)


def recover_interrupted(root: Path, say: Callable[[str], None] | None = None) -> bool:
    """Finish or refuse a restore of ``root`` that was killed. True if there was one to recover. Takes the sidecar lock
    (so a restore that is still running is never touched) and raises plain-worded errors when it cannot be sure."""
    root = Path(os.path.realpath(root))
    if journal_restore_pending(root) is None:
        return False
    side = _Sidecar(root)
    side.open()
    try:
        _recover(root, side, say or _say_stderr)
        side.finish()
    finally:
        side.close()
    return True
