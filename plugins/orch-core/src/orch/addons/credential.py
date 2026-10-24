"""The one key an add-on may hold (V3 G6, design-g6 section 5): the ONLY reader and writer of
``<state>/hosts/<ws>/addons/<name>/credential``.

* The person stores it with ``orch addon login <name>`` (tty only, see ``orch.cli.addon_credential``); no host op, no
  dashboard form and no hub path writes it, so an agent cannot plant one.
* The file is ``0600`` in a ``0700`` directory of the host UID, written tmp + fsync + rename through directory fds
  (``O_NOFOLLOW``, ``O_EXCL``); a loose mode, a link, a second hard link or another owner is refused, never repaired.
* :func:`provider` hands the runner a callable that yields the value only while the ACTIVE grant holds both
  ``credential`` and ``network`` (C2) and the file belongs to that add-on; otherwise ``None`` (the add-on is called
  without a key).
* :func:`remove` and :func:`sweep` delete it (purge, uninstall, a grant that dropped the capability, host start; C4):
  idempotent.

Tier A limit (spec 15.1): a same-UID agent can read the file; a per-add-on UID (#287) closes that."""

from __future__ import annotations

import contextlib
import hmac
import json
import os
import re
import secrets
import stat
from collections.abc import Callable, Iterable, Iterator
from pathlib import Path

from orch.host import custody
from orch.market import ADDONS  # the store-layout lint matches the bare word

FILE = "credential"
NAME = re.compile(r"[a-z][a-z0-9-]{0,39}")
MAX_LEN = 1024
_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
_FILE_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC


class Secret:
    """The value, opaque: formatting, pickling and copying it cannot disclose it."""

    __slots__ = ("__value",)
    __hash__ = None  # type: ignore[assignment]

    def __init__(self, value: str) -> None:
        if not valid(value):
            raise ValueError("invalid credential") from None
        self.__value = value

    def __repr__(self) -> str:
        return "<credential>"

    __str__ = __repr__

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, Secret):
            return NotImplemented
        return hmac.compare_digest(self.__value, other.__value)

    def __reduce__(self):
        raise TypeError("credentials cannot be serialized") from None

    def __getstate__(self):
        raise TypeError("credentials cannot be serialized") from None

    def __copy__(self):
        raise TypeError("credentials cannot be copied") from None

    def __deepcopy__(self, memo):
        raise TypeError("credentials cannot be copied") from None

    def reveal(self) -> str:
        return self.__value


def valid(value: object) -> bool:
    """Shape check only: printable ASCII, 1 to 1024 characters (no space)."""
    return type(value) is str and 1 <= len(value) <= MAX_LEN and all("!" <= c <= "~" for c in value)


def _name(name: str) -> str:
    if type(name) is not str or NAME.fullmatch(name) is None:
        raise ValueError("invalid add-on name")
    return name


def root_dir(state_dir, workspace_id: str) -> Path:
    return Path(state_dir) / "hosts" / workspace_id / ADDONS


def addon_dir(state_dir, workspace_id: str, name: str) -> Path:
    return root_dir(state_dir, workspace_id) / _name(name)


@contextlib.contextmanager
def _open_dir(directory: Path, host_uid: int, *, create: bool = False) -> Iterator[int | None]:
    """``directory`` as an fd checked on the fd itself (a private directory of the host UID), or ``None`` if absent.
    With ``create`` the missing directory (and its ``addons`` parent) is made ``0700``."""
    if create:
        for d in (directory.parent, directory):
            try:
                os.mkdir(d, 0o700)
            except FileExistsError:
                pass
    try:
        fd = os.open(directory, _DIR_FLAGS)
    except FileNotFoundError:
        yield None
        return
    except OSError:
        raise custody.unsafe("credential custody requires a private directory") from None
    try:
        info = os.fstat(fd)
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != host_uid or stat.S_IMODE(info.st_mode) != 0o700:
            raise custody.unsafe("credential custody requires a private directory")
        try:
            now = os.lstat(directory)
        except OSError:
            raise custody.unsafe("credential custody directory changed") from None
        if (now.st_dev, now.st_ino) != (info.st_dev, info.st_ino):
            raise custody.unsafe("credential custody directory changed")
        yield fd
    finally:
        os.close(fd)


def _unlink(dfd: int, name: str) -> bool:
    try:
        os.unlink(name, dir_fd=dfd)
    except FileNotFoundError:
        return False
    return True


def _read(dfd: int, host_uid: int) -> bytes | None:
    try:
        fd = os.open(FILE, _FILE_FLAGS, dir_fd=dfd)
    except FileNotFoundError:
        return None
    except OSError:
        raise custody.unsafe("credential custody requires a regular 0600 file") from None
    with os.fdopen(fd, "rb") as handle:
        info = os.fstat(handle.fileno())
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != host_uid
            or stat.S_IMODE(info.st_mode) != 0o600
            or info.st_nlink != 1
        ):
            raise custody.unsafe("credential custody requires a regular 0600 file")
        raw = handle.read(4097)
    if len(raw) > 4096:
        raise custody.unsafe("unreadable credential")
    return raw


def _parse(raw: bytes, name: str) -> Secret | None:
    """The stored value, or ``None`` when the file belongs to another add-on (``addon`` != ``name``)."""
    try:
        doc = json.loads(raw)
    except (ValueError, RecursionError):
        raise custody.unsafe("unreadable credential") from None
    if not isinstance(doc, dict) or type(doc.get("v")) is not int or doc["v"] != 1 or not valid(doc.get("value")):
        raise custody.unsafe("invalid stored credential") from None
    if doc.get("addon") != name:
        return None
    return Secret(doc["value"])


def load(state_dir, workspace_id: str, host_uid: int, name: str) -> Secret | None:
    """The stored credential of add-on ``name``, ``None`` when absent or stored for another name. A compromised file is
    refused (``custody.unsafe_mode``), never repaired."""
    try:
        with _open_dir(addon_dir(state_dir, workspace_id, name), host_uid) as dfd:
            if dfd is None:
                return None
            raw = _read(dfd, host_uid)
    except (OSError, RecursionError):
        raise custody.unsafe("unreadable credential") from None
    return None if raw is None else _parse(raw, name)


def status(state_dir, workspace_id: str, host_uid: int, name: str) -> str:
    """``"stored"`` or ``"missing"``: existence only, the value is never read into the answer."""
    return "stored" if load(state_dir, workspace_id, host_uid, name) is not None else "missing"


def store(state_dir, workspace_id: str, host_uid: int, name: str, secret: Secret, now: float) -> None:
    """Write (or replace) the credential of ``name``: tmp + fsync + rename inside the checked directory."""
    directory = addon_dir(state_dir, workspace_id, name)
    custody.check_tree(directory.parent.parent, host_uid)
    data = json.dumps(
        {"v": 1, "addon": name, "value": secret.reveal(), "stored_at": int(now)}, sort_keys=True, separators=(",", ":")
    ).encode()
    with _open_dir(directory, host_uid, create=True) as dfd:
        if dfd is None:
            raise custody.unsafe("credential custody requires a private directory")
        tmp = f".{FILE}.{os.getpid()}.{secrets.token_hex(4)}.tmp"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600, dir_fd=dfd)
        try:
            with os.fdopen(fd, "wb") as handle:
                os.fchmod(handle.fileno(), 0o600)
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, FILE, src_dir_fd=dfd, dst_dir_fd=dfd)
        except BaseException:
            _unlink(dfd, tmp)
            raise
        os.fsync(dfd)


def remove(state_dir, workspace_id: str, name: str) -> bool:
    """Delete the credential directory of ``name``; ``False`` when there was none. Idempotent. Only the credential file
    and leftover temp files are unlinked (a planted foreign entry keeps the directory and is a custody finding)."""
    directory = addon_dir(state_dir, workspace_id, name)
    try:
        with _open_dir(directory, os.getuid()) as dfd:
            if dfd is None:
                return False
            existed = False
            for entry in os.listdir(dfd):
                if entry == FILE or (entry.startswith(f".{FILE}.") and entry.endswith(".tmp")):
                    existed |= _unlink(dfd, entry) and entry == FILE
        with contextlib.suppress(OSError):  # not empty (foreign entry): left for the custody check to report
            os.rmdir(directory)
    except custody.HostError:
        # a directory that is not ours or not private cannot be trusted to unlink inside; remove the link/dir itself
        _force(directory)
        return True
    return existed


def _force(directory: Path) -> None:
    """Remove a compromised credential directory entry without following links."""
    try:
        info = os.lstat(directory)
    except FileNotFoundError:
        return
    if stat.S_ISDIR(info.st_mode):
        for entry in os.listdir(directory):
            with contextlib.suppress(OSError):
                os.unlink(directory / entry)
        with contextlib.suppress(OSError):
            os.rmdir(directory)
    else:
        with contextlib.suppress(OSError):
            os.unlink(directory)


def sweep(state_dir, workspace_id: str, keep: Iterable[str]) -> list[str]:
    """Delete the credential directory of every add-on NOT in ``keep`` (``keep``: names that are granted, not purged and
    hold ``credential`` in their grant). Called after a purge, an uninstall or a grant that dropped the capability, and
    at host start (C4). Returns the names removed."""
    wanted = set(keep)
    base = root_dir(state_dir, workspace_id)
    try:
        entries = sorted(os.listdir(base))
    except (FileNotFoundError, NotADirectoryError):
        return []
    gone = []
    for entry in entries:
        if entry in wanted or NAME.fullmatch(entry) is None:
            continue
        remove(state_dir, workspace_id, entry)
        gone.append(entry)
    return gone


def provider(
    state_dir, workspace_id: str, host_uid: int, name: str, capabilities: Iterable[str], *, active: bool = True
) -> Callable[[], str | None]:
    """The callable the runner calls just before one call of add-on ``name`` (C2). It yields the value only when the
    ACTIVE grant (``active``, ``capabilities``) holds both ``credential`` and ``network`` and the stored file is this
    add-on's; in every other case ``None``, without reading the file at all when the grant does not qualify."""
    caps = frozenset(capabilities)
    qualifies = active and "credential" in caps and "network" in caps

    def read() -> str | None:
        if not qualifies:
            return None
        secret = load(state_dir, workspace_id, host_uid, name)
        return None if secret is None else secret.reveal()

    return read
