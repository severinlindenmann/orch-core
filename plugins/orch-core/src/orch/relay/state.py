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

import json
import os
import re
import secrets
import stat
import threading
from pathlib import Path
from typing import Any, Protocol

__all__ = ["FileSignLedger", "PersistedOnce", "SignLedger", "StateDir", "StateError"]

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
    def __init__(self, path: str | os.PathLike[str]) -> None:
        self.path = Path(path)
        try:
            os.mkdir(self.path, 0o700)
        except FileExistsError:
            pass
        st = os.lstat(self.path)
        if not stat.S_ISDIR(st.st_mode) or st.st_uid != os.getuid() or stat.S_IMODE(st.st_mode) != 0o700:
            raise StateError(f"relay state directory {self.path} must be a 0700 directory owned by this user")
        self._lock = threading.Lock()

    def _dir(self) -> int:
        try:
            return os.open(self.path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        except OSError as e:
            raise StateError(f"cannot open {self.path}") from e

    @staticmethod
    def _name(name: str) -> str:
        if type(name) is not str or not _NAME.fullmatch(name):
            raise StateError("bad state file name")
        return name

    def read(self, name: str) -> bytes | None:
        name = self._name(name)
        dfd = self._dir()
        try:
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
        finally:
            os.close(dfd)

    def write(self, name: str, data: bytes) -> None:
        name = self._name(name)
        if len(data) > MAX_FILE:
            raise StateError(f"{name}: too large")
        with self._lock:
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
                os.rename(tmp, name, src_dir_fd=dfd, dst_dir_fd=dfd)  # replaces a symlink entry, never follows it
                os.fsync(dfd)
            except BaseException:
                try:
                    os.unlink(tmp, dir_fd=dfd)
                except OSError:
                    pass
                raise
            finally:
                os.close(dfd)

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
        self._lock = threading.Lock()

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
        with self._lock:
            obj = self._load()
            old = obj.get(kind, {}).get(key)
            if old is not None and old.get("digest") != digest:
                raise StateError("sign ledger: another digest is already recorded for this key")
            obj.setdefault(kind, {})[key] = {"digest": digest, "sig": sig}
            self._s.write_json(self.FILE, obj)


class PersistedOnce:
    """Write-once bytes under ``name``: :meth:`keep` stores ``data`` if nothing is stored and returns what is."""

    def __init__(self, state: StateDir, name: str) -> None:
        self._s, self._name = state, name
        self._lock = threading.Lock()

    def get(self) -> bytes | None:
        return self._s.read(self._name)

    def keep(self, data: bytes) -> bytes:
        with self._lock:
            held = self._s.read(self._name)
            if held is not None:
                return held
            self._s.write(self._name, data)
            return data
