"""Durable relay state for R1 (contract 11A.3): a private directory, files written atomically, never through a symlink.

* :class:`StateDir`: the directory is ``0700`` and owned by this UID (created so if missing; refused otherwise), and
  its ancestors pass the custody tree check (round 6, orch-52 #2): walked from ``/`` with ``O_NOFOLLOW | O_DIRECTORY``
  (a symlinked ancestor is refused), each owned by root or this UID and not group/other-writable unless it is a
  root-owned sticky directory; the directory is then created and opened relative to its checked parent. ACLs are not
  inspected (``ctypes`` is import-banned here; the gap is documented in 11A.3). Every
  file is opened relative to a directory fd with ``O_NOFOLLOW``, must be a regular file owned by this UID with mode
  ``0600``, and is written as a temp file (``O_CREAT | O_EXCL | O_NOFOLLOW``, ``0600``) + ``fsync`` + ``rename`` +
  directory ``fsync``. A symlink, a foreign owner or a wider mode is refused (:class:`StateError`), never repaired.
* :class:`FileSignLedger`: the last-signed ledger of :class:`orch.wsproto.sign.RelaySigner` (review M2). An entry is
  durable before the signature leaves the signer.
* :class:`PersistedOnce`: write-once bytes (the genesis pin and the card of the first link, review L1): the first value
  is kept and every later call gets the stored bytes back.

**For orch-52 / R5:** 11A.3 says relay state is written with the host's custody helpers
(``host/custody.write_private``, ``check_tree``). ``orch.host`` is import-banned in ``orch.relay``, so this module is a
stdlib implementation of the same rules (``_walk_to_parent``/``_check_ancestor`` mirror ``check_tree``, without the ACL
check); R5 may swap it for the host helpers behind the same interfaces.
"""

from __future__ import annotations

import json
import logging
import os
import re
import secrets
import stat
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from orch import oslock

__all__ = [
    "FileHolderStore",
    "FileSignLedger",
    "HolderRecord",
    "HolderStore",
    "PersistedOnce",
    "SignLedger",
    "StateDir",
    "StateError",
    "new_instance_id",
    "regenerate_instance_id",
]

log = logging.getLogger("orch.wsproto.state")

MAX_FILE = 1 << 20
BIG_FILE = 8 << 20  # R3's staging manifest (<= 4 MiB) and restore records (a final body <= 2.6 MiB plus its source)
_NAME = re.compile(r"[a-z0-9][a-z0-9._-]{0,63}")
_TEMP = re.compile(r"\.[a-z0-9][a-z0-9._-]{0,63}\.[0-9a-f]{16}\.tmp")
_KEY = re.compile(r"[a-z0-9_.:-]{1,64}")
# per-kind key rule of the sign ledger, on top of _KEY: a card is keyed by str(card_seq), the canonical decimal (round
# 10, R9-1). "010", " 10", "+10", "1_0" or a fullwidth digit would be int()-equal to a seq yet missed by the exact
# lookup, so a different card could be signed at that seq; the reader accepts only what record() writes. R6a: a
# member list by str(list_seq), a wk_commit by str(epoch), a grant by "<device hex>:<epoch>", all canonical.
_DECIMAL = re.compile(r"0|[1-9][0-9]*")
_LEDGER_KEY = {
    "card": _DECIMAL,
    "member_list": _DECIMAL,
    "wk_commit": _DECIMAL,
    "wk_grant": re.compile(r"[0-9a-f]{32}:(0|[1-9][0-9]*)"),
    "cert_request": re.compile(r"[0-9a-f]{32}"),  # R6c: one signed request per request_id (11A.15.7)
}


def _limit(limit: int) -> int:
    if type(limit) is not int or not 0 < limit <= BIG_FILE:
        raise StateError("bad state file limit")
    return limit


class StateError(Exception):
    """The state directory or a file in it is unsafe or unreadable. Messages name the file, never its contents."""


class _Held:
    """The lock fd and the ``RLock`` of one open :class:`StateDir`, kept apart from it so the fork hook can reach
    them without holding the instance (``weakref`` is import-banned in ``orch.relay``)."""

    __slots__ = ("fd", "lock")

    def __init__(self, fd: int) -> None:
        self.fd: int | None = fd
        self.lock = threading.RLock()


_OPEN: set[_Held] = set()  # every StateDir not yet closed; a closed one is removed, so its fd number is never touched


def _after_fork_in_child() -> None:
    """Child side of ``os.fork``: close the child's copy of every lock fd with a plain ``close``, never
    ``flock(LOCK_UN)`` (the flock belongs to the open file description the parent shares, so an unlock would release
    the parent's lock), and replace each ``RLock``: one another parent thread held at the fork is never released in
    the child."""
    for h in list(_OPEN):
        h.lock = threading.RLock()
        fd, h.fd = h.fd, None
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass
    _OPEN.clear()


_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC


def _me() -> int:
    """This process's UID (one seam, so a test can simulate a foreign owner)."""
    return os.getuid()


def _check_ancestor(fd: int, shown: Path, uid: int) -> None:
    """One ancestor of the state directory: owned by root or this UID, and not writable by group or other unless it is
    a root-owned sticky directory (``/tmp``). The same rule as ``orch.host.custody._check_component`` (round 6,
    orch-52 #2), minus its ACL check (it needs ``ctypes``, import-banned here; the documented 11A.3 gap)."""
    st = os.fstat(fd)
    mode = stat.S_IMODE(st.st_mode)
    if st.st_uid not in (0, uid):
        raise StateError(f"{shown}: owner {st.st_uid}, expected root or {uid}")
    sticky = st.st_uid == 0 and bool(mode & stat.S_ISVTX)
    if mode & 0o022 and not sticky:
        raise StateError(f"{shown}: writable by another principal")


def _walk_to_parent(path: Path) -> int:
    """Open every ancestor of ``path`` from ``/`` one component at a time with ``O_NOFOLLOW | O_DIRECTORY`` (a symlink
    anywhere on the way is refused), check each with :func:`_check_ancestor`, and return an fd of the parent: the
    state directory itself is then created and opened relative to it, never by path (``check_tree`` mirrored)."""
    if not path.is_absolute() or ".." in path.parts or len(path.parts) < 2:
        raise StateError(f"{path}: expected an absolute path without parent traversal")
    uid = _me()
    fd = os.open("/", _DIR_FLAGS)
    try:
        _check_ancestor(fd, Path("/"), uid)
        current = Path("/")
        for part in path.parts[1:-1]:
            current /= part
            try:
                nxt = os.open(part, _DIR_FLAGS, dir_fd=fd)
            except FileNotFoundError:
                raise StateError(f"{current} does not exist") from None
            except OSError as e:  # ELOOP/ENOTDIR: a symlinked or non-directory ancestor
                raise StateError(
                    f"{current}: a symlink or not a directory ({e.strerror or e}); pass the resolved path (on macOS "
                    "/tmp, /var and $TMPDIR are symlinks: use /private/tmp/..., /private/var/...)"
                ) from None
            os.close(fd)
            fd = nxt
            _check_ancestor(fd, current, uid)
    except BaseException:
        os.close(fd)
        raise
    return fd


class SignLedger(Protocol):
    """What the signer records before it returns a signature: ``(kind, key) -> (digest, sig)``."""

    def get(self, kind: str, key: str) -> tuple[str, str] | None: ...

    def keys(self, kind: str) -> list[str]: ...

    def record(self, kind: str, key: str, digest: str, sig: str) -> None: ...


class StateDir:
    """One live owner per directory: the constructor takes an exclusive, non-blocking ``flock`` on the directory for
    the object's lifetime (review D1). A second ``StateDir`` on the same directory, in this process or another, gets
    :class:`StateError` "in use"; :meth:`close` releases it. ``lock`` serialises every read-modify-write inside the
    owning process, so two ledgers or write-once files on one directory cannot lose an update.

    Every operation works relative to the locked directory fd, never by path again: if the directory is renamed and a
    new one created at the old path, this instance keeps writing into the directory it locked (review round 3, R3).
    The lock fd is close-on-exec, so an exec'd child never holds it; ``os.fork`` does inherit it, so the instance
    records its pid and refuses every operation in a forked child (R2). An ``after_in_child`` fork hook also closes
    the child's copy of the fd (a plain ``close``, never ``LOCK_UN``: the flock belongs to the open file description
    the parent shares, so an unlock would release the parent's lock) and marks the instance unusable, so a forked
    child that lives on does not keep the directory locked after the parent closes it (review round 4, N3; narrow
    race: a fork by another thread inside ``close()`` or ``__init__`` can leave the child holding the lock until it
    exits, availability only, round 5 P2)."""

    def __init__(self, path: str | os.PathLike[str]) -> None:
        if ".." in Path(path).parts:  # before abspath, which would fold it away lexically
            raise StateError(f"{path}: expected a path without parent traversal")
        self.path = Path(os.path.abspath(path))
        parent = _walk_to_parent(self.path)
        name = self.path.name
        try:
            try:
                os.mkdir(name, 0o700, dir_fd=parent)
                os.fsync(parent)  # the new entry is durable (R6a round 2, F2: a lost directory entry is state loss)
            except FileExistsError:
                pass
            except OSError as e:
                raise StateError(f"cannot create {self.path}") from e
            try:
                fd = os.open(name, _DIR_FLAGS, dir_fd=parent)
            except OSError as e:  # ELOOP/ENOTDIR: a symlink or not a directory
                why = f"relay state directory {self.path} must be a 0700 directory owned by this user"
                raise StateError(why) from e
        finally:
            os.close(parent)
        try:
            st = os.fstat(fd)
            if not stat.S_ISDIR(st.st_mode) or st.st_uid != _me() or stat.S_IMODE(st.st_mode) != 0o700:
                raise StateError(f"relay state directory {self.path} must be a 0700 directory owned by this user")
            oslock.flock(fd, oslock.LOCK_EX | oslock.LOCK_NB)
        except BlockingIOError:
            os.close(fd)
            raise StateError(f"relay state directory {self.path} is in use by another instance") from None
        except OSError as e:
            os.close(fd)
            raise StateError(f"cannot lock {self.path}") from e
        except StateError:
            os.close(fd)
            raise
        self._h = _Held(fd)
        self._pid = os.getpid()
        _OPEN.add(self._h)

    @property
    def lock(self) -> threading.RLock:
        """Serialises every read-modify-write on this directory within the owning process."""
        return self._h.lock

    @property
    def _held(self) -> int | None:
        return self._h.fd

    def close(self) -> None:
        """Release the directory (closing the fd drops the ``flock``). In a forked child this does nothing: the fork
        hook already closed the child's copy, and the parent's lock must stay."""
        if os.getpid() != self._pid:
            return
        with self.lock:
            fd, self._h.fd = self._h.fd, None
            _OPEN.discard(self._h)
            if fd is not None:
                os.close(fd)

    def __enter__(self) -> StateDir:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def _dir(self) -> int:
        """The locked directory fd; call with ``lock`` held."""
        if os.getpid() != self._pid:
            raise StateError(f"relay state directory {self.path} belongs to the parent process (forked child)")
        if self._held is None:
            raise StateError(f"relay state directory {self.path} is closed")
        return self._held

    @staticmethod
    def _name(name: str) -> str:
        if type(name) is not str or not _NAME.fullmatch(name):
            raise StateError("bad state file name")
        return name

    def read(self, name: str, *, limit: int = MAX_FILE) -> bytes | None:
        """The file's bytes, or ``None`` when it does not exist. ``limit`` (default 1 MiB, at most :data:`BIG_FILE`)
        bounds what is read; R3's manifest and restore records are larger (11A.3)."""
        name = self._name(name)
        with self.lock:
            return self._read(name, self._dir(), _limit(limit))

    def _read(self, name: str, dfd: int, limit: int = MAX_FILE) -> bytes | None:
        try:
            fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=dfd)
        except FileNotFoundError:
            return None
        except OSError as e:  # ELOOP: a symlink
            raise StateError(f"{name}: not a regular file") from e
        try:
            st = os.fstat(fd)
            if not stat.S_ISREG(st.st_mode) or st.st_uid != _me() or stat.S_IMODE(st.st_mode) != 0o600:
                raise StateError(f"{name}: must be a 0600 regular file owned by this user")
            if st.st_size > limit:
                raise StateError(f"{name}: too large")
            chunks, n = [], 0
            while piece := os.read(fd, 65536):
                n += len(piece)
                if n > limit:
                    raise StateError(f"{name}: too large")
                chunks.append(piece)
            return b"".join(chunks)
        finally:
            os.close(fd)

    def identity(self) -> tuple[int, int]:
        """``(st_dev, st_ino)`` of the locked directory. The instance record is bound to ``ino`` (a copy or a restore
        through a backup tool is another directory); ``dev`` is recorded for information only."""
        with self.lock:
            st = os.fstat(self._dir())
            return st.st_dev, st.st_ino

    def write(self, name: str, data: bytes, *, limit: int = MAX_FILE) -> None:
        name = self._name(name)
        self._publish(name, data, claim=False, limit=_limit(limit))

    def claim(self, name: str, data: bytes) -> bool:
        """Create ``name`` with ``data`` only if it does not exist (``os.link`` of a complete temp file: atomic, never
        replaces); ``False`` when another writer claimed it first."""
        return self._publish(self._name(name), data, claim=True)

    def _publish(self, name: str, data: bytes, *, claim: bool, limit: int = MAX_FILE) -> bool:
        if len(data) > limit:
            raise StateError(f"{name}: too large")
        with self.lock:
            dfd = self._dir()
            tmp = f".{name}.{secrets.token_hex(8)}.tmp"
            try:
                fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=dfd)
                try:
                    os.fchmod(fd, 0o600)
                    view = memoryview(data)
                    while view:
                        view = view[os.write(fd, view) :]
                    os.fsync(fd)
                finally:
                    os.close(fd)
                if claim:
                    try:
                        os.link(tmp, name, src_dir_fd=dfd, dst_dir_fd=dfd, follow_symlinks=False)
                    except FileExistsError:
                        return False
                    finally:
                        os.unlink(tmp, dir_fd=dfd)
                else:
                    os.rename(tmp, name, src_dir_fd=dfd, dst_dir_fd=dfd)  # replaces a symlink entry, never follows it
                os.fsync(dfd)
                return True
            except BaseException:
                try:
                    os.unlink(tmp, dir_fd=dfd)
                except OSError:
                    pass
                raise

    def remove(self, name: str) -> bool:
        """Unlink ``name`` relative to the locked directory fd (never by path, never through a symlink: the entry
        itself is removed). ``False`` when it did not exist."""
        name = self._name(name)
        with self.lock:
            dfd = self._dir()
            try:
                os.unlink(name, dir_fd=dfd)
            except FileNotFoundError:
                return False
            os.fsync(dfd)
            return True

    def open_private(self, name: str) -> int:
        """The checked open of 11A.3 step 2 for a file a library opens by path (``rids.sqlite`` and its sidecars):
        ``O_RDWR | O_CREAT | O_NOFOLLOW`` (0600) relative to the locked directory fd, then ``fstat``: a regular file
        of this UID, mode 0600, ``nlink == 1``. Returns the fd; the caller keeps it open and compares its
        ``(st_dev, st_ino)`` with a ``stat`` of :meth:`file_path` after the library opened the path (step 4)."""
        name = self._name(name)
        with self.lock:
            dfd = self._dir()
            try:
                fd = os.open(name, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600, dir_fd=dfd)
            except OSError as e:  # ELOOP: a symlink
                raise StateError(f"{name}: not a regular file") from e
            st = os.fstat(fd)
            if (
                not stat.S_ISREG(st.st_mode)
                or st.st_uid != _me()
                or stat.S_IMODE(st.st_mode) != 0o600
                or st.st_nlink != 1
            ):
                os.close(fd)
                raise StateError(f"{name}: must be a 0600 regular file of this user with one link")
            return fd

    def names(self) -> list[str]:
        """The state file names here (R6a: ``membership/keys/`` and ``cards/`` are scanned for EHW recovery and
        orphans), sorted; a crashed write's temp file (``.<name>.<hex>.tmp``) is never listed."""
        with self.lock:
            return sorted(n for n in os.listdir(self._dir()) if _NAME.fullmatch(n))

    def discard_temps(self) -> int:
        """Unlink every temp file a crashed write left here (``.<name>.<hex>.tmp``; a key file's temp holds the
        key). Safe for the lock holder only, which every caller is (one live instance per directory)."""
        n = 0
        with self.lock:
            dfd = self._dir()
            for name in os.listdir(dfd):
                if _TEMP.fullmatch(name):
                    try:
                        os.unlink(name, dir_fd=dfd)
                        n += 1
                    except FileNotFoundError:
                        pass
            if n:
                os.fsync(dfd)
        return n

    def file_path(self, name: str) -> Path:
        """The path of ``name`` here, only for a library that opens by path, together with :meth:`open_private`."""
        return self.path / self._name(name)

    def read_json(self, name: str) -> Any:
        """The parsed file, or ``None`` only when it does not exist; unreadable, not JSON or ``null``: StateError."""
        raw = self.read(name)
        if raw is None:
            return None
        try:
            obj = json.loads(raw)
        except ValueError as e:
            raise StateError(f"{name}: not JSON") from e
        if obj is None:  # None means "the file does not exist" to every caller, never "it says null" (round 8, R7-1)
            raise StateError(f"{name}: null")
        return obj

    def write_json(self, name: str, obj: Any) -> None:
        self.write(name, json.dumps(obj, sort_keys=True, separators=(",", ":")).encode())


os.register_at_fork(after_in_child=_after_fork_in_child)


def _ledger_key_ok(kind: str, key: str) -> bool:
    """``key`` matches ``_KEY`` and its kind's own rule (``card``: canonical decimal, ASCII digits only)."""
    rule = _LEDGER_KEY.get(kind)
    return bool(_KEY.fullmatch(key)) and (rule is None or bool(rule.fullmatch(key)))


class FileSignLedger:
    """``sign-ledger.json``: ``{kind: {key: {"digest", "sig"}}}``, rewritten atomically on every record."""

    FILE = "sign-ledger.json"

    def __init__(self, state: StateDir) -> None:
        self._s = state

    def _load(self) -> dict[str, dict[str, dict[str, str]]]:
        """The whole ledger, every kind and entry type-checked: a ``null`` or malformed kind or entry (``{"card":
        {"10": null}}``, ``{"card": []}``, a non-string digest or sig, a kind or key ``record`` would never write,
        such as card ``"010"``) is a StateError, never "nothing signed", so a different card can never be signed at
        that seq (rounds 9 and 10, fail closed)."""
        obj = self._s.read_json(self.FILE)
        if obj is None:
            return {}
        if type(obj) is not dict:
            raise StateError("sign ledger shape")
        for kind, entries in obj.items():
            if type(entries) is not dict:
                raise StateError("sign ledger shape")
            if not _KEY.fullmatch(kind) or any(not _ledger_key_ok(kind, k) for k in entries):
                raise StateError("sign ledger key")
            for e in entries.values():
                if type(e) is not dict or type(e.get("digest")) is not str or type(e.get("sig")) is not str:
                    raise StateError("sign ledger entry")
        return obj

    def get(self, kind: str, key: str) -> tuple[str, str] | None:
        e = self._load().get(kind, {}).get(key)
        if e is None:  # only a missing key: _load refused every null entry
            return None
        return e["digest"], e["sig"]

    def keys(self, kind: str) -> list[str]:
        return sorted(self._load().get(kind, {}))

    def record(self, kind: str, key: str, digest: str, sig: str) -> None:
        if not _KEY.fullmatch(kind) or not _ledger_key_ok(kind, key):
            raise StateError("sign ledger key")
        with self._s.lock:
            obj = self._load()
            old = obj.get(kind, {}).get(key)
            if old is not None and old.get("digest") != digest:
                raise StateError("sign ledger: another digest is already recorded for this key")
            obj.setdefault(kind, {})[key] = {"digest": digest, "sig": sig}
            self._s.write_json(self.FILE, obj)


class PersistedOnce:
    """Write-once bytes under ``name``: :meth:`keep` claims the file with ``os.link`` (never a rename, review D1) and
    returns whatever is stored, so concurrent keepers all get the first value."""

    def __init__(self, state: StateDir, name: str) -> None:
        self._s, self._name = state, name

    def get(self) -> bytes | None:
        return self._s.read(self._name)

    def keep(self, data: bytes) -> bytes:
        if self._s.claim(self._name, data):
            return data
        held = self._s.read(self._name)
        if held is None:
            raise StateError(f"{self._name}: claimed but unreadable")
        return held


# --- holder.json and the instance id (11A.3; restart takeover decided by orch-52, 13 Oct 2026) ------------------

_INSTANCE = re.compile(r"[0-9a-f]{32}")


@dataclass(frozen=True)
class HolderRecord:
    """``holder.json``: whether this state directory was the last holder, its ``gen``, the instance id it was written
    under, and ``pending`` (the expected ``gen`` of a takeover probe in flight, written before the probe is sent)."""

    last_holder: bool
    gen: int | None
    instance_id: str
    pending: int | None = None


class HolderStore(Protocol):
    def instance_id(self) -> str:
        """This state directory's instance id."""
        ...

    def binding_problem(self) -> str | None:
        """``None`` if the instance record is bound to this very directory; else the cause (copied or restored)."""
        ...

    def regenerate(self) -> str:
        """A new instance id bound to this directory (the operator path only)."""
        ...

    def load(self) -> HolderRecord | None: ...

    def history_problem(self) -> str | None:
        """Asked by the preflight only when ``holder.json`` is absent: ``None`` if that is a true first link, else the
        cause. ``link.json`` already past ``linking`` (``linked``) while ``holder.json`` and/or ``instance.json`` is
        missing means a restored or copied linked directory, which must not pass as a fresh first link (round 7,
        R6-3). ``linking`` with both absent stays legitimate."""
        ...

    def save(self, rec: HolderRecord) -> None:
        """Durable before it returns."""
        ...


def new_instance_id() -> str:
    """A fresh random 128-bit instance id (32 lower-case hex)."""
    return secrets.token_hex(16)


def _instance_record(state: StateDir, iid: str) -> bytes:
    dev, ino = state.identity()
    return json.dumps({"instance_id": iid, "dev": dev, "ino": ino}, sort_keys=True).encode()


def regenerate_instance_id(state: StateDir) -> str:
    """Give a state directory a new instance id and bind it to this directory's ``(dev, ino)`` (atomic write). Only the
    operator path calls this: ``orch host relay takeover`` and the restore procedure, which **MUST** call it on a
    directory restored from a backup or copied, so the copy never claims the original's holdership."""
    iid = new_instance_id()
    state.write(FileHolderStore.INSTANCE, _instance_record(state, iid))
    return iid


class FileHolderStore:
    """``holder.json`` and ``instance.json`` in the relay state directory. ``instance.json`` is ``{instance_id, dev,
    ino}``: the id, created with the state (claimed once with ``os.link``), bound to the locked directory's inode.
    A copy (``cp -r``, rsync) carries the id verbatim but not the inode, and a restore through a backup tool recreates
    the directory: both show as a binding problem. A process restart or a rename within one filesystem keeps the
    inode. ``dev`` is recorded but not compared (review round 4, N2): ``st_dev`` is assigned per mount and can change
    across a reboot or remount (APFS volume order, btrfs subvolumes, LVM, external volumes), so a changed ``dev`` is
    only logged. A move across filesystems, or a tool that recreates the directory, needs the operator path
    (:func:`regenerate_instance_id`).

    Not detected (residuals, 11A.3): a block-level clone (VM image, ``asr``/``dd``) keeps the inode and every byte, a
    writable snapshot or clone attached on the SAME machine (btrfs/ZFS snapshot, an ``asr``/``dd`` clone as a second
    volume) keeps the inode and gets a new ``st_dev``, so since the device check was dropped it is no longer caught
    (review round 5 P1), and a copy onto another volume can land on the same inode number by chance. No local check
    can prove a state directory is unique; the relay's holder ``gen`` is the backstop."""

    FILE = "holder.json"
    INSTANCE = "instance.json"
    LINK = "link.json"

    def __init__(self, state: StateDir) -> None:
        self._s = state
        # True when instance.json did not exist and this very open created it (round 7, R6-3)
        self._fresh_instance = state.claim(self.INSTANCE, _instance_record(state, new_instance_id()))

    def _instance(self) -> dict[str, Any]:
        obj = self._s.read_json(self.INSTANCE)
        if (
            type(obj) is not dict
            or type(obj.get("instance_id")) is not str
            or not _INSTANCE.fullmatch(obj["instance_id"])
        ):
            raise StateError("instance.json shape")
        if type(obj.get("dev")) is not int or type(obj.get("ino")) is not int:
            raise StateError(
                "instance.json has no directory binding (an older format): run `orch host relay takeover` "
                "(operator_takeover), which regenerates it with regenerate_instance_id"
            )
        return obj

    def instance_id(self) -> str:
        return self._instance()["instance_id"]

    def binding_problem(self) -> str | None:
        obj = self._instance()
        dev, ino = self._s.identity()
        if obj["ino"] != ino:
            return "this state directory was copied or restored (its instance record names another directory)"
        if obj["dev"] != dev:
            log.info("relay state directory device number changed (remount or reboot); not a copy, inode unchanged")
        return None

    def regenerate(self) -> str:
        return regenerate_instance_id(self._s)

    def load(self) -> HolderRecord | None:
        obj = self._s.read_json(self.FILE)
        if obj is None:
            return None
        try:
            rec = HolderRecord(obj["last_holder"], obj["gen"], obj["instance_id"], obj["pending"])
        except (KeyError, TypeError):
            raise StateError("holder.json shape") from None
        ok_int = lambda v: v is None or (type(v) is int and v >= 0)  # noqa: E731
        if type(rec.last_holder) is not bool or not ok_int(rec.gen) or not ok_int(rec.pending):
            raise StateError("holder.json shape")
        if type(rec.instance_id) is not str:
            raise StateError("holder.json shape")
        return rec

    def history_problem(self) -> str | None:
        link = self._s.read_json(self.LINK)
        if link is None:
            return None  # nothing linked yet (first_link refuses to start without link.json anyway)
        state = link.get("state") if type(link) is dict else None
        if state == "linking":
            return None  # a true first link (or its resume): holder.json comes with the first hold
        missing = "holder.json and instance.json are" if self._fresh_instance else "holder.json is"
        shown = state if type(state) is str and len(state) <= 32 else "?"
        return (
            f"link.json says state {shown!r} but {missing} missing: a restored or copied linked state directory, "
            "not a first link"
        )

    def save(self, rec: HolderRecord) -> None:
        self._s.write_json(
            self.FILE,
            {"last_holder": rec.last_holder, "gen": rec.gen, "instance_id": rec.instance_id, "pending": rec.pending},
        )
