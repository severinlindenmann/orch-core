"""Durable relay state for R1 (contract 11A.3): a private directory, files written atomically, never through a symlink.

* :class:`StateDir`: the directory is ``0700`` and owned by this UID (created so if missing; refused otherwise). Every
  file is opened relative to a directory fd with ``O_NOFOLLOW``, must be a regular file owned by this UID with mode
  ``0600``, and is written as a temp file (``O_CREAT | O_EXCL | O_NOFOLLOW``, ``0600``) + ``fsync`` + ``rename`` +
  directory ``fsync``. A symlink, a foreign owner or a wider mode is refused (:class:`StateError`), never repaired.
* :class:`FileSignLedger`: the last-signed ledger of :class:`orch.relay.sign.RelaySigner` (review M2). An entry is
  durable before the signature leaves the signer.
* :class:`PersistedOnce`: write-once bytes (the genesis pin and the card of the first link, review L1): the first value
  is kept and every later call gets the stored bytes back.

**For orch-52 / R5:** 11A.3 says relay state is written with the host's custody helpers
(``host/custody.write_private``, ``check_tree``). ``orch.host`` is import-banned in ``orch.relay``, so this module is a
stdlib implementation of the same rules; R5 may swap it for the host helpers behind the same interfaces.
"""

from __future__ import annotations

import fcntl
import json
import os
import re
import secrets
import stat
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

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

MAX_FILE = 1 << 20
_NAME = re.compile(r"[a-z0-9][a-z0-9._-]{0,63}")
_KEY = re.compile(r"[a-z0-9_.:-]{1,64}")


class StateError(Exception):
    """The state directory or a file in it is unsafe or unreadable. Messages name the file, never its contents."""


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
    records its pid and refuses every operation in a forked child (R2)."""

    def __init__(self, path: str | os.PathLike[str]) -> None:
        self.path = Path(path)
        try:
            os.mkdir(self.path, 0o700)
        except FileExistsError:
            pass
        st = os.lstat(self.path)
        if not stat.S_ISDIR(st.st_mode) or st.st_uid != os.getuid() or stat.S_IMODE(st.st_mode) != 0o700:
            raise StateError(f"relay state directory {self.path} must be a 0700 directory owned by this user")
        try:
            fd = os.open(self.path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
        except OSError as e:
            raise StateError(f"cannot open {self.path}") from e
        try:
            held = os.fstat(fd)
            if (held.st_dev, held.st_ino) != (st.st_dev, st.st_ino):
                raise StateError(f"relay state directory {self.path} changed while it was opened")
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(fd)
            raise StateError(f"relay state directory {self.path} is in use by another instance") from None
        except OSError as e:
            os.close(fd)
            raise StateError(f"cannot lock {self.path}") from e
        except StateError:
            os.close(fd)
            raise
        self._held: int | None = fd
        self._pid = os.getpid()
        self.lock = threading.RLock()

    def close(self) -> None:
        """Release the directory (closing the fd drops the ``flock``). In a forked child this only forgets the fd."""
        with self.lock:
            fd, self._held = self._held, None
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

    def read(self, name: str) -> bytes | None:
        name = self._name(name)
        with self.lock:
            return self._read(name, self._dir())

    def _read(self, name: str, dfd: int) -> bytes | None:
        try:
            fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=dfd)
        except FileNotFoundError:
            return None
        except OSError as e:  # ELOOP: a symlink
            raise StateError(f"{name}: not a regular file") from e
        try:
            st = os.fstat(fd)
            if not stat.S_ISREG(st.st_mode) or st.st_uid != os.getuid() or stat.S_IMODE(st.st_mode) != 0o600:
                raise StateError(f"{name}: must be a 0600 regular file owned by this user")
            if st.st_size > MAX_FILE:
                raise StateError(f"{name}: too large")
            chunks, n = [], 0
            while piece := os.read(fd, 65536):
                n += len(piece)
                if n > MAX_FILE:
                    raise StateError(f"{name}: too large")
                chunks.append(piece)
            return b"".join(chunks)
        finally:
            os.close(fd)

    def identity(self) -> tuple[int, int]:
        """``(st_dev, st_ino)`` of the locked directory: what the instance record is bound to (a copy or a restore
        through a backup tool is another directory)."""
        with self.lock:
            st = os.fstat(self._dir())
            return st.st_dev, st.st_ino

    def write(self, name: str, data: bytes) -> None:
        name = self._name(name)
        self._publish(name, data, claim=False)

    def claim(self, name: str, data: bytes) -> bool:
        """Create ``name`` with ``data`` only if it does not exist (``os.link`` of a complete temp file: atomic, never
        replaces); ``False`` when another writer claimed it first."""
        return self._publish(self._name(name), data, claim=True)

    def _publish(self, name: str, data: bytes, *, claim: bool) -> bool:
        if len(data) > MAX_FILE:
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

    def read_json(self, name: str) -> Any:
        raw = self.read(name)
        if raw is None:
            return None
        try:
            return json.loads(raw)
        except ValueError as e:
            raise StateError(f"{name}: not JSON") from e

    def write_json(self, name: str, obj: Any) -> None:
        self.write(name, json.dumps(obj, sort_keys=True, separators=(",", ":")).encode())


class FileSignLedger:
    """``sign-ledger.json``: ``{kind: {key: {"digest", "sig"}}}``, rewritten atomically on every record."""

    FILE = "sign-ledger.json"

    def __init__(self, state: StateDir) -> None:
        self._s = state

    def _load(self) -> dict[str, dict[str, dict[str, str]]]:
        obj = self._s.read_json(self.FILE)
        if obj is None:
            return {}
        if type(obj) is not dict:
            raise StateError("sign ledger shape")
        return obj

    def get(self, kind: str, key: str) -> tuple[str, str] | None:
        e = self._load().get(kind, {}).get(key)
        if e is None:
            return None
        if type(e) is not dict or type(e.get("digest")) is not str or type(e.get("sig")) is not str:
            raise StateError("sign ledger entry")
        return e["digest"], e["sig"]

    def keys(self, kind: str) -> list[str]:
        return sorted(self._load().get(kind, {}))

    def record(self, kind: str, key: str, digest: str, sig: str) -> None:
        if not _KEY.fullmatch(kind) or not _KEY.fullmatch(key):
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
    ino}``: the id, created with the state (claimed once with ``os.link``), bound to the locked directory's identity.
    A copy (``cp -r``, rsync) carries the id verbatim but not the inode, and a restore through a backup tool recreates
    the directory: both show as a binding problem. A restart or a rename within one filesystem keeps ``(dev, ino)``.
    A move across filesystems, or a tool that recreates the directory, needs the operator path
    (:func:`regenerate_instance_id`)."""

    FILE = "holder.json"
    INSTANCE = "instance.json"

    def __init__(self, state: StateDir) -> None:
        self._s = state
        state.claim(self.INSTANCE, _instance_record(state, new_instance_id()))

    def _instance(self) -> dict[str, Any]:
        obj = self._s.read_json(self.INSTANCE)
        if (
            type(obj) is not dict
            or type(obj.get("instance_id")) is not str
            or not _INSTANCE.fullmatch(obj["instance_id"])
            or type(obj.get("dev")) is not int
            or type(obj.get("ino")) is not int
        ):
            raise StateError("instance.json shape")
        return obj

    def instance_id(self) -> str:
        return self._instance()["instance_id"]

    def binding_problem(self) -> str | None:
        obj = self._instance()
        if (obj["dev"], obj["ino"]) != self._s.identity():
            return "this state directory was copied or restored (its instance record names another directory)"
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

    def save(self, rec: HolderRecord) -> None:
        self._s.write_json(
            self.FILE,
            {"last_holder": rec.last_holder, "gen": rec.gen, "instance_id": rec.instance_id, "pending": rec.pending},
        )
