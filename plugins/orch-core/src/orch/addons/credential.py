"""The one key an add-on may hold (V3 G6, design-g6 section 5): the ONLY reader and writer of
``<state>/hosts/<ws>/addons/<name>/credential``.

* The person stores it with ``orch addon login <name>`` (tty only, see ``orch.cli.addon_credential``); no host op,
  no dashboard form and no hub path writes it, and orch does not hand it to agents.
* The file is ``0600`` in a ``0700`` directory of the host UID. ``addons/`` is opened once (``O_NOFOLLOW``) and every
  operation is relative to that fd (tmp + fsync + rename, ``O_EXCL``);
  a loose mode, a link, a second hard link or another owner is refused, never repaired.
* :func:`provider` hands the runner a callable that yields the value only while the ACTIVE grant holds both
  ``credential`` and ``network`` (C2) and the file belongs to that add-on; otherwise ``None`` (the add-on is called
  without a key).
* :func:`remove` and :func:`sweep` delete it (purge, uninstall, a grant that dropped the capability, host start; C4):
  idempotent.

Tier A limit (spec 15.1): an agent running as the same UID can read or write the file directly (and a terminal is no
barrier to a determined one) until the per-add-on UID (#287)."""

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
from orch.host.errors import HostError
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


def _check_fd(fd: int, host_uid: int, what: str) -> None:
    info = os.fstat(fd)
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != host_uid or stat.S_IMODE(info.st_mode) != 0o700:
        raise custody.unsafe(f"credential custody requires a private {what}")


def _open_at(dir_fd: int, name: str, host_uid: int, what: str, *, create: bool = False) -> int | None:
    """``name`` below ``dir_fd`` as a checked directory fd (``O_NOFOLLOW | O_DIRECTORY``, 0700, the host UID), or
    ``None`` if absent. Never follows a link, never reaches a path: the only way the module opens a directory."""
    if create:
        try:
            os.mkdir(name, 0o700, dir_fd=dir_fd)
        except FileExistsError:
            pass
    try:
        fd = os.open(name, _DIR_FLAGS, dir_fd=dir_fd)
    except FileNotFoundError:
        return None
    except OSError:
        raise custody.unsafe(f"credential custody requires a private {what}") from None
    try:
        _check_fd(fd, host_uid, what)
    except BaseException:
        os.close(fd)
        raise
    return fd


@contextlib.contextmanager
def _root(state_dir, workspace_id: str, host_uid: int, *, create: bool = False) -> Iterator[int | None]:
    """The ``addons`` directory of the workspace as a checked fd, opened ONCE (``O_NOFOLLOW``) below ``hosts/<ws>``
    (itself opened ``O_NOFOLLOW``, and with ``create`` first walked by ``check_tree``); ``None`` if absent. Every
    operation of this module is relative to it, so a link planted at ``addons`` or below it is refused, and nothing
    outside the directory that was checked is ever read, written or deleted."""
    hosts = Path(state_dir) / "hosts" / workspace_id
    if create:
        custody.check_tree(hosts, host_uid)
    try:
        hfd = os.open(hosts, _DIR_FLAGS)
    except FileNotFoundError:
        if create:
            raise custody.unsafe("credential custody requires the workspace directory") from None
        yield None
        return
    except OSError:
        raise custody.unsafe("credential custody requires the workspace directory") from None
    rfd = None
    try:
        rfd = _open_at(hfd, ADDONS, host_uid, "directory", create=create)
        yield rfd
    finally:
        if rfd is not None:
            os.close(rfd)
        os.close(hfd)


@contextlib.contextmanager
def _addon(state_dir, workspace_id: str, host_uid: int, name: str, *, create: bool = False) -> Iterator[int | None]:
    """The directory of add-on ``name`` as a checked fd, or ``None`` if absent."""
    _name(name)
    with _root(state_dir, workspace_id, host_uid, create=create) as rfd:
        if rfd is None:
            yield None
            return
        dfd = _open_at(rfd, name, host_uid, "directory", create=create)
        try:
            yield dfd
        finally:
            if dfd is not None:
                os.close(dfd)


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
        with _addon(state_dir, workspace_id, host_uid, name) as dfd:
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
    data = json.dumps(
        {"v": 1, "addon": name, "value": secret.reveal(), "stored_at": int(now)}, sort_keys=True, separators=(",", ":")
    ).encode()
    with _addon(state_dir, workspace_id, host_uid, name, create=True) as dfd:
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
    """Delete the credential directory of ``name``; ``False`` when there was none. Idempotent. Relative to the checked
    ``addons`` fd: an entry that is a link or a regular file is unlinked; a directory is entered only if it is private
    and the host's (only the credential file and our own temp leftovers are unlinked in it); any other directory is left
    alone and refused (``custody.unsafe_mode``), never emptied."""
    _name(name)
    uid = os.getuid()
    with _root(state_dir, workspace_id, uid) as rfd:
        return False if rfd is None else _remove_at(rfd, name, uid)


def _remove_at(rfd: int, name: str, uid: int) -> bool:
    try:
        info = os.stat(name, dir_fd=rfd, follow_symlinks=False)
    except FileNotFoundError:
        return False
    if not stat.S_ISDIR(info.st_mode):  # a link or a file planted where the directory belongs: not followed
        _unlink(rfd, name)
        return True
    dfd = _open_at(rfd, name, uid, "directory")  # refuses a loose or foreign directory: left as it is
    if dfd is None:
        return False
    try:
        existed = False
        for entry in os.listdir(dfd):
            if entry == FILE or (entry.startswith(f".{FILE}.") and entry.endswith(".tmp")):
                try:
                    existed |= _unlink(dfd, entry) and entry == FILE
                except OSError:  # a directory of that name: not ours to empty, rmdir below fails and custody reports
                    pass
    finally:
        os.close(dfd)
    with contextlib.suppress(OSError):  # not empty (a foreign entry): left for the custody check to report
        os.rmdir(name, dir_fd=rfd)
    return existed


def sweep(state_dir, workspace_id: str, keep: Iterable[str]) -> list[str]:
    """Delete the credential directory of every add-on NOT in ``keep`` (``keep``: names that are granted, not purged and
    hold ``credential`` in their grant). Called after a purge, an uninstall or a grant that dropped the capability, and
    at host start (C4). Returns the names removed. A compromised entry is skipped and refused after the rest was
    swept (the first ``custody.unsafe_mode`` is raised at the end)."""
    wanted = set(keep)
    uid = os.getuid()
    gone: list[str] = []
    first: HostError | None = None
    with _root(state_dir, workspace_id, uid) as rfd:
        if rfd is None:
            return gone
        for entry in sorted(os.listdir(rfd)):
            if entry in wanted or NAME.fullmatch(entry) is None:
                continue
            try:
                _remove_at(rfd, entry, uid)
            except HostError as exc:
                first = first or exc
                continue
            gone.append(entry)
    if first is not None:
        raise first
    return gone


def provider(
    state_dir, workspace_id: str, host_uid: int, name: str, capabilities: Iterable[str], *, active: bool = True
) -> Callable[[str], str | None]:
    """The callable the runner calls, as ``read(granted.name)``, just before a call of ``name`` (C2); it is bound
    to ``name`` and refuses a call for any other add-on. It yields the value only when the
    ACTIVE grant (``active``, ``capabilities``) holds both ``credential`` and ``network`` and the stored file is this
    add-on's; in every other case ``None``, without reading the file at all when the grant does not qualify."""
    caps = frozenset(capabilities)
    qualifies = active and "credential" in caps and "network" in caps

    def read(called: str) -> str | None:
        if called != name:  # the runner passes the add-on it is calling: a provider of another one is refused
            raise ValueError("this credential provider belongs to another add-on")
        if not qualifies:
            return None
        secret = load(state_dir, workspace_id, host_uid, name)
        return None if secret is None else secret.reveal()

    return read
