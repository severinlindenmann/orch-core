"""The ``convenience`` custody backend (orch-v2-host §0.8.6, D-P2-23): a person's device key held **without any
factor**.

Any process running as the person can make it sign. That is the accepted phase-1 risk ("Convenience signing: agents
running as you, or anything that takes over this dashboard, could sign."); every event it signs declares
``auth: "convenience"`` (F1 §5.3, §11.4).

Where the key lives (one key per workspace and device; the value is the 32-byte P-256 scalar):

* macOS: the traditional login keychain (:mod:`orch.custody.keychain`), service ``io.orch.device-key``, account
  ``<workspace_id>/<device_id>``. Encrypted at rest; may be in Time Machine backups and migrations.
* other Unix: :class:`FileStore`, a 0600 file at ``<person state>/person/<ws>/devices/<device>.key`` beneath
  person-owned 0700 directories, validated through descriptor-relative opens (``O_NOFOLLOW``, ``fstat``).
* Windows: unsupported in phase 1 (:class:`~orch.custody.base.BackendUnavailable`).

This module is **never** imported by :mod:`orch.custody`'s package init, by the host (the host audit hook refuses the
import) or by the dashboard's HTTP process. Only :mod:`orch.custody.select`, ``orch keys`` and the click signer
(:mod:`orch.click`) load it. Nothing here returns key bytes to a caller outside :mod:`orch.custody`.
"""

from __future__ import annotations

import json
import os
import re
import stat
import sys
from pathlib import Path
from typing import Any, Protocol

from orch import canon, crypto

from .base import (
    DEVICE_KEY_LABELS,
    BackendUnavailable,
    CustodyError,
    KeyExists,
    KeyNotFound,
    check_key_id,
    label_allowed,
)

__all__ = [
    "AUTH",
    "ConvenienceBackend",
    "FakeStore",
    "FileStore",
    "KeychainAdapter",
    "Store",
    "open_store",
    "person_root",
    "secure_dir",
]

AUTH = "convenience"
WS_ID = re.compile(r"[0-9a-f]{32}")
DEVICE = re.compile(r"d_[0-9a-f]{32}")
SCALAR_LEN = 32


def _check_ids(workspace_id: str, device: str) -> None:
    if type(workspace_id) is not str or not WS_ID.fullmatch(workspace_id):
        raise CustodyError("not a workspace id")
    if type(device) is not str or not DEVICE.fullmatch(device):
        raise CustodyError("not a device id")


def person_root(state_dir: str | os.PathLike[str]) -> Path:
    """``<person state>/person``: the person's own custody root, never ``<state>/hosts/<ws>/`` (the host's list)."""
    return Path(state_dir) / "person"


class Store(Protocol):
    """Opaque storage of one 32-byte scalar per (workspace, device). ``get`` is the only call that returns secret
    bytes, and only to :class:`ConvenienceBackend` and :mod:`orch.custody.select`'s transitions."""

    kind: str

    def put(self, workspace_id: str, device: str, secret: bytes, *, prefix: str) -> None: ...

    def get(self, workspace_id: str, device: str) -> bytes: ...

    def exists(self, workspace_id: str, device: str) -> bool: ...

    def delete(self, workspace_id: str, device: str) -> None: ...


# --- descriptor-relative private directories (Unix) ------------------------------------------------------------------

_DIR_FLAGS = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)


def _check_ancestors(path: Path) -> None:
    """Every existing ancestor of ``path``: a real directory (no symlink), owned by root or the person, and not
    writable by group or other unless sticky. Ordinary readable ancestors such as ``/`` and ``/Users`` pass."""
    uid = os.getuid()
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current = current / part
        try:
            st = os.lstat(current)
        except FileNotFoundError:
            return
        if stat.S_ISLNK(st.st_mode):
            # macOS: /var and /tmp are root-owned symlinks into /private; accept only those, never a user's link
            if st.st_uid == 0 and current.parent == Path("/"):
                continue
            raise CustodyError(f"{current} is a symbolic link; custody paths must not contain one")
        if not stat.S_ISDIR(st.st_mode):
            raise CustodyError(f"{current} is not a directory")
        if st.st_uid not in (0, uid):
            raise CustodyError(f"{current} belongs to another user")
        if st.st_mode & 0o022 and not st.st_mode & stat.S_ISVTX:
            raise CustodyError(f"{current} is writable by others")


def _check_private_fd(fd: int, what: str) -> None:
    st = os.fstat(fd)
    if not stat.S_ISDIR(st.st_mode):
        raise CustodyError(f"{what} is not a directory")
    if st.st_uid != os.getuid():
        raise CustodyError(f"{what} belongs to another user")
    if st.st_mode & 0o077:
        raise CustodyError(f"{what} is accessible by others (mode {st.st_mode & 0o777:03o}); it must be 0700")


def secure_dir(root: Path, parts: tuple[str, ...], *, create: bool) -> int:
    """An fd of ``root/parts…``, walked with ``O_NOFOLLOW`` from ``root``: ``root`` and every part are person-owned
    0700 directories (created so when ``create``), and ``root``'s ancestors pass :func:`_check_ancestors`. The caller
    closes the fd."""
    if os.name == "nt":  # pragma: no cover
        raise BackendUnavailable("convenience custody is not supported on Windows in phase 1")
    root = Path(os.path.abspath(root))
    _check_ancestors(root.parent)
    if create:
        try:
            os.mkdir(root, 0o700)
        except FileExistsError:
            pass
    try:
        fd = os.open(root, _DIR_FLAGS)
    except FileNotFoundError:
        raise KeyNotFound(str(root.name)) from None
    except OSError as exc:
        raise CustodyError(f"cannot open {root}: {exc.strerror}") from None
    try:
        _check_private_fd(fd, str(root))
        for part in parts:
            if not part or part in (".", "..") or "/" in part:
                raise CustodyError("bad path component")
            if create:
                try:
                    os.mkdir(part, 0o700, dir_fd=fd)
                except FileExistsError:
                    pass
            try:
                nxt = os.open(part, _DIR_FLAGS, dir_fd=fd)
            except FileNotFoundError:
                raise KeyNotFound(part) from None
            except OSError as exc:
                raise CustodyError(f"cannot open {part}: {exc.strerror}") from None
            os.close(fd)
            fd = nxt
            _check_private_fd(fd, part)
        return fd
    except BaseException:
        os.close(fd)
        raise


def read_private(dir_fd: int, name: str, *, limit: int = 64 * 1024) -> bytes:
    """A regular, person-owned, 0600, single-link file in ``dir_fd``, read through its own descriptor."""
    try:
        fd = os.open(name, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0), dir_fd=dir_fd)
    except FileNotFoundError:
        raise KeyNotFound(name) from None
    except OSError as exc:
        raise CustodyError(f"cannot open {name}: {exc.strerror}") from None
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise CustodyError(f"{name} is not a regular file")
        if st.st_uid != os.getuid():
            raise CustodyError(f"{name} belongs to another user")
        if st.st_mode & 0o077:
            raise CustodyError(f"{name} is accessible by others (mode {st.st_mode & 0o777:03o}); it must be 0600")
        if st.st_nlink != 1:
            raise CustodyError(f"{name} has another hard link")
        if st.st_size > limit:
            raise CustodyError(f"{name} is too large")
        chunks = []
        while True:
            b = os.read(fd, 65536)
            if not b:
                break
            chunks.append(b)
        return b"".join(chunks)
    finally:
        os.close(fd)


def write_private(dir_fd: int, name: str, data: bytes, *, replace: bool) -> None:
    """Write ``data`` durably as ``name`` (0600) in ``dir_fd``: a temporary file, ``fsync``, then linked into place
    (``replace=False``: exclusive, :class:`KeyExists` when taken) or renamed over it (``replace=True``); then the
    directory is synced."""
    tmp = f".{name}.tmp{os.getpid()}.{os.urandom(4).hex()}"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    fd = os.open(tmp, flags, 0o600, dir_fd=dir_fd)
    try:
        try:
            os.write(fd, data)
            os.fsync(fd)
        finally:
            os.close(fd)
        if replace:
            os.rename(tmp, name, src_dir_fd=dir_fd, dst_dir_fd=dir_fd)
        else:
            try:
                os.link(tmp, name, src_dir_fd=dir_fd, dst_dir_fd=dir_fd, follow_symlinks=False)
            except FileExistsError:
                raise KeyExists(name) from None
    finally:
        try:
            os.unlink(tmp, dir_fd=dir_fd)
        except FileNotFoundError:
            pass
    try:
        os.fsync(dir_fd)
    except OSError:  # pragma: no cover - some file systems refuse a directory fsync
        pass


def unlink_private(dir_fd: int, name: str) -> None:
    try:
        os.unlink(name, dir_fd=dir_fd)
    except FileNotFoundError:
        raise KeyNotFound(name) from None
    try:
        os.fsync(dir_fd)
    except OSError:  # pragma: no cover
        pass


# --- stores ----------------------------------------------------------------------------------------------------------


class FileStore:
    """Unix: ``<root>/<ws>/devices/<device>.key`` (0600), JSON ``{"v":1,"workspace_id","device","d"}``."""

    kind = "file"

    def __init__(self, root: str | os.PathLike[str]) -> None:
        if os.name == "nt":  # pragma: no cover
            raise BackendUnavailable("convenience custody is not supported on Windows in phase 1")
        self.root = Path(os.path.abspath(root))

    def _dir(self, workspace_id: str, *, create: bool) -> int:
        return secure_dir(self.root, (workspace_id, "devices"), create=create)

    def put(self, workspace_id: str, device: str, secret: bytes, *, prefix: str = "") -> None:
        _check_ids(workspace_id, device)
        if type(secret) is not bytes or len(secret) != SCALAR_LEN:
            raise CustodyError("the secret is a 32-byte scalar")
        doc = {"v": 1, "workspace_id": workspace_id, "device": device, "d": crypto.b64u(secret)}
        fd = self._dir(workspace_id, create=True)
        try:
            write_private(fd, f"{device}.key", json.dumps(doc, sort_keys=True).encode("ascii"), replace=False)
        finally:
            os.close(fd)

    def get(self, workspace_id: str, device: str) -> bytes:
        _check_ids(workspace_id, device)
        fd = self._dir(workspace_id, create=False)
        try:
            raw = read_private(fd, f"{device}.key")
        finally:
            os.close(fd)
        try:
            doc = json.loads(raw)
            if set(doc) != {"v", "workspace_id", "device", "d"} or doc["v"] != 1:
                raise ValueError("shape")
            if doc["workspace_id"] != workspace_id or doc["device"] != device:
                raise ValueError("the file names another workspace or device")
            return crypto.unb64u(doc["d"], SCALAR_LEN)
        except (ValueError, KeyError, TypeError, crypto.CryptoError) as exc:
            raise CustodyError(f"corrupt convenience key file: {type(exc).__name__}") from None

    def exists(self, workspace_id: str, device: str) -> bool:
        _check_ids(workspace_id, device)
        try:
            fd = self._dir(workspace_id, create=False)
        except KeyNotFound:
            return False
        try:
            os.stat(f"{device}.key", dir_fd=fd, follow_symlinks=False)
            return True
        except FileNotFoundError:
            return False
        finally:
            os.close(fd)

    def delete(self, workspace_id: str, device: str) -> None:
        """Best effort: unlinks the managed copy. It does not erase backups, snapshots or copy-on-write blocks."""
        _check_ids(workspace_id, device)
        fd = self._dir(workspace_id, create=False)
        try:
            unlink_private(fd, f"{device}.key")
        finally:
            os.close(fd)


class KeychainAdapter:
    """macOS: one generic-password item per (workspace, device) in an explicitly selected keychain."""

    kind = "keychain"

    def __init__(self, keychain_path: str | os.PathLike[str] | None = None) -> None:
        from .keychain import KeychainStore  # darwin only; never imported by the host or the HTTP process

        self._kc = KeychainStore(keychain_path)

    @staticmethod
    def _account(workspace_id: str, device: str) -> str:
        _check_ids(workspace_id, device)
        return f"{workspace_id}/{device}"

    def put(self, workspace_id: str, device: str, secret: bytes, *, prefix: str = "") -> None:
        if type(secret) is not bytes or len(secret) != SCALAR_LEN:
            raise CustodyError("the secret is a 32-byte scalar")
        label = f"orch device key {re.sub(r'[^A-Z0-9]', '', prefix)[:10]} {device}".replace("  ", " ")
        self._kc.put(
            self._account(workspace_id, device),
            secret,
            label=label,
            description="orch convenience signing key (no presence)",
        )

    def get(self, workspace_id: str, device: str) -> bytes:
        secret = self._kc.get(self._account(workspace_id, device))
        if len(secret) != SCALAR_LEN:
            raise CustodyError("the keychain item is not a 32-byte scalar")
        return secret

    def exists(self, workspace_id: str, device: str) -> bool:
        return self._kc.exists(self._account(workspace_id, device))

    def delete(self, workspace_id: str, device: str) -> None:
        self._kc.delete(self._account(workspace_id, device))


class FakeStore:
    """In memory, for tests only (never chosen by :func:`open_store`)."""

    kind = "fake"

    def __init__(self) -> None:
        self.items: dict[tuple[str, str], bytes] = {}
        self.gets = 0
        self.fail: BaseException | None = None

    def put(self, workspace_id: str, device: str, secret: bytes, *, prefix: str = "") -> None:
        _check_ids(workspace_id, device)
        if (workspace_id, device) in self.items:
            raise KeyExists(device)
        if type(secret) is not bytes or len(secret) != SCALAR_LEN:
            raise CustodyError("the secret is a 32-byte scalar")
        self.items[(workspace_id, device)] = secret

    def get(self, workspace_id: str, device: str) -> bytes:
        self.gets += 1
        if self.fail is not None:
            raise self.fail
        try:
            return self.items[(workspace_id, device)]
        except KeyError:
            raise KeyNotFound(device) from None

    def exists(self, workspace_id: str, device: str) -> bool:
        return (workspace_id, device) in self.items

    def delete(self, workspace_id: str, device: str) -> None:
        if self.items.pop((workspace_id, device), None) is None:
            raise KeyNotFound(device)


def open_store(state_dir: str | os.PathLike[str]) -> Store:
    """The platform's store: the login keychain on macOS, the 0600 file elsewhere on Unix; Windows is refused. Tests
    replace this function (``orch.custody.convenience.open_store``) with a :class:`FakeStore` or a :class:`FileStore`
    or a keychain adapter over a throwaway keychain; nothing in production passes a store."""
    if os.name == "nt":  # pragma: no cover
        raise BackendUnavailable("convenience custody is not supported on Windows in phase 1 (direct mode there)")
    if sys.platform == "darwin":
        return KeychainAdapter()
    return FileStore(person_root(state_dir))


# --- the backend -----------------------------------------------------------------------------------------------------


def _zeroise(buf: bytearray) -> None:
    for i in range(len(buf)):
        buf[i] = 0


#: device labels this backend never signs. H12d-2 decision (orch-v2-host §0.8.6 item 9.3): a reader declaration is a
#: reviewed statement about the running build, so it is signed only with a review (the passphrase prompt shows every
#: field); a key that signs for any same-UID process without presence must not raise or lower a reader's level. A
#: convenience device needs no declaration of its own for its signatures: it is the signing device of its convenience
#: checks, qualified by ``reader_version`` (this host's model).
_UNREVIEWED = (canon.LABELS["sig_reader_decl"].encode("ascii"),)


class ConvenienceBackend:
    """The person-capable backend that asks for no factor. It holds one device key (``dk_sig``) for one workspace,
    signs only device labels, and checks the loaded key against the device the selection names on every load."""

    name = AUTH
    auth = AUTH
    person_capable = True

    def __init__(self, store: Store, workspace_id: str, device: str, pub: bytes) -> None:
        from orch.identity import device_ref

        _check_ids(workspace_id, device)
        crypto.validate_public_key(pub)
        if device_ref(pub) != device:
            raise CustodyError("the selection's public key does not belong to its device")
        self.store, self.workspace_id, self.device, self._pub = store, workspace_id, device, pub
        self.signatures = 0

    def create(self, key_id: str, *, secret: bytes | None = None, role: str = "device") -> bytes:
        raise CustodyError("a convenience key is made by converting a device key (orch keys custody convenience)")

    def public_key(self, key_id: str) -> bytes:
        check_key_id(key_id)
        return self._pub  # from the selection: no secret is read, nothing prompts

    def _load(self) -> Any:
        scalar = bytearray(self.store.get(self.workspace_id, self.device))
        try:
            if len(scalar) != SCALAR_LEN:
                raise CustodyError("the stored key is not a 32-byte scalar")
            key = crypto.private_key_from_scalar(int.from_bytes(scalar, "big"))
        except crypto.CryptoError:
            raise CustodyError("the stored key is not a P-256 key") from None
        finally:
            _zeroise(scalar)
        if crypto.public_bytes(key) != self._pub:
            raise CustodyError("the stored key does not match the selected device")
        return key

    def sign(self, key_id: str, payload: bytes, *, action: str) -> bytes:
        check_key_id(key_id)
        if not label_allowed(payload, DEVICE_KEY_LABELS):
            raise CustodyError("a device key does not sign this payload (unknown label or another role's label)")
        if label_allowed(payload, _UNREVIEWED):  # #463 review L1, decided in H12d-2: never without a review
            raise CustodyError(
                "a convenience device key does not sign reader declarations (no review, no presence); declare with "
                "a passphrase device key (orch keys reader declare)"
            )
        key = self._load()
        try:
            sig = crypto.sign(key, payload)
        finally:
            key = None  # noqa: F841
        self.signatures += 1
        return sig

    def unseal(self, key_id: str, purpose: str, object_id: bytes, epoch: int, extra: bytes, sealed: bytes) -> bytes:
        """Never: convenience custody holds one device signing key, and a key-exchange key is never in convenience
        custody (the primary's keys are passphrase-only, orch-v2-host 11A.15.1)."""
        raise CustodyError("convenience custody holds no key-exchange key and opens nothing")

    def verify_capability(self) -> None:
        """Load the key and prove it signs (a throwaway payload, verified against the selected public key)."""
        key = self._load()
        probe = b"orch/v2/custody-probe|" + os.urandom(16)
        if not crypto.verify(self._pub, crypto.sign(key, probe), probe):
            raise CustodyError("the stored key does not sign for its public key")

    def presence(self) -> str:
        return AUTH

    def exists(self, key_id: str) -> bool:
        return self.store.exists(self.workspace_id, self.device)

    def delete(self, key_id: str) -> None:
        self.store.delete(self.workspace_id, self.device)
