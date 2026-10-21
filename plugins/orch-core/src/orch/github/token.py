"""Keep credentials opaque so incidental formatting cannot disclose them."""

from __future__ import annotations

import contextlib
import hashlib
import hmac
import json
import os
import secrets
import stat
from pathlib import Path

from orch.host import custody

# This is the only module that opens the token file.


class NoToken(Exception):
    """No token is stored. Carries no message and no path: the host maps it to ``github.no_token``."""

    def __init__(self) -> None:
        super().__init__("no github token")


class Token:
    """Explicit revelation prevents accidental logging and serialization of credentials."""

    __slots__ = ("__value",)
    __hash__ = None

    def __init__(self, value: str) -> None:
        if type(value) is not str or not 1 <= len(value) <= 255 or any(not "!" <= c <= "~" for c in value):
            raise ValueError("invalid github token") from None
        self.__value = value

    def __repr__(self) -> str:
        return "<github token>"

    __str__ = __repr__

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, Token):
            return NotImplemented
        return hmac.compare_digest(self.__value, other.__value)

    def __reduce__(self):
        raise TypeError("github tokens cannot be serialized") from None

    def __getstate__(self):
        raise TypeError("github tokens cannot be serialized") from None

    def __copy__(self):
        raise TypeError("github tokens cannot be copied") from None

    def __deepcopy__(self, memo):
        raise TypeError("github tokens cannot be copied") from None

    def reveal(self) -> str:
        """Only the HTTP client needs the plaintext to authenticate a request."""
        return self.__value

    def fingerprint(self) -> str:
        """Partition future cache entries by credential without storing the credential."""
        return hashlib.sha256(self.__value.encode("ascii")).hexdigest()


TOKEN_FILE = "token"
CACHE_FILES = ("cache.json", "etags.json")


def github_dir(state_dir, workspace_id) -> Path:
    return Path(state_dir) / "hosts" / workspace_id / "github"


_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
_FILE_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC


@contextlib.contextmanager
def _open_dir(directory: Path, host_uid: int, *, create: bool = False):
    """The github directory as an fd (``O_NOFOLLOW | O_DIRECTORY``), checked on the fd itself, or ``None`` if absent.

    Every later file operation goes through this fd, so swapping the path for a link or another directory after the
    check changes nothing: the fd keeps pointing at the directory that was checked.
    """
    created = False
    if create:
        try:
            os.mkdir(directory, 0o700)
            created = True
        except FileExistsError:
            pass
    try:
        fd = os.open(directory, _DIR_FLAGS)
    except FileNotFoundError:
        yield None
        return
    except OSError:
        raise custody.unsafe("GitHub custody requires a private directory") from None
    try:
        info = os.fstat(fd)
        if created:
            os.fchmod(fd, 0o700)
            info = os.fstat(fd)
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != host_uid or stat.S_IMODE(info.st_mode) != 0o700:
            raise custody.unsafe("GitHub custody requires a private directory")
        custody.check_tree(directory, host_uid)
        try:
            now = os.lstat(directory)
        except OSError:
            raise custody.unsafe("GitHub custody directory changed") from None
        if (now.st_dev, now.st_ino) != (info.st_dev, info.st_ino):
            raise custody.unsafe("GitHub custody directory changed")
        yield fd
    finally:
        os.close(fd)


def _unlink(dfd: int, name: str) -> bool:
    try:
        os.unlink(name, dir_fd=dfd)
    except FileNotFoundError:
        return False
    return True


def load(state_dir, workspace_id, host_uid) -> Token:
    """Refuse compromised custody rather than silently repairing a disclosed credential."""
    try:
        with _open_dir(github_dir(state_dir, workspace_id), host_uid) as dfd:
            if dfd is None:
                raise NoToken()
            try:
                fd = os.open(TOKEN_FILE, _FILE_FLAGS, dir_fd=dfd)
            except FileNotFoundError:
                raise NoToken() from None
            except OSError:
                raise custody.unsafe("GitHub custody requires a regular 0600 file") from None
            with os.fdopen(fd, "rb") as handle:
                info = os.fstat(handle.fileno())
                if (
                    not stat.S_ISREG(info.st_mode)
                    or info.st_uid != host_uid
                    or stat.S_IMODE(info.st_mode) != 0o600
                    or info.st_nlink != 1
                ):
                    raise custody.unsafe("GitHub custody requires a regular 0600 file")
                raw = handle.read(4097)
        if len(raw) > 4096:
            raise custody.unsafe("Unreadable GitHub credential")
        try:
            doc = json.loads(raw)
        except ValueError:
            raise custody.unsafe("Unreadable GitHub credential") from None
    except (OSError, RecursionError):
        raise custody.unsafe("Unreadable GitHub credential") from None
    valid = isinstance(doc, dict) and type(doc.get("v")) is int and doc["v"] == 1
    value = doc.get("token") if valid else None
    try:
        token = Token(value)
        valid = value.startswith("github_pat_")
    except ValueError:
        valid = False
    if not valid:
        raise custody.unsafe("Invalid stored GitHub credential") from None
    return token


def store(state_dir, workspace_id, host_uid, token: Token) -> None:
    """Rotation discards cached responses so credentials never inherit another account's data."""
    directory = github_dir(state_dir, workspace_id)
    custody.check_tree(directory.parent, host_uid)
    if not token.reveal().startswith("github_pat_"):
        raise custody.unsafe("Invalid GitHub credential")
    data = json.dumps({"v": 1, "token": token.reveal()}, sort_keys=True, separators=(",", ":")).encode()
    with _open_dir(directory, host_uid, create=True) as dfd:
        if dfd is None:
            raise custody.unsafe("GitHub custody requires a private directory")
        for name in CACHE_FILES:
            _unlink(dfd, name)
        tmp = f".{TOKEN_FILE}.{os.getpid()}.{secrets.token_hex(4)}.tmp"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600, dir_fd=dfd)
        try:
            with os.fdopen(fd, "wb") as handle:
                os.fchmod(handle.fileno(), 0o600)
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, TOKEN_FILE, src_dir_fd=dfd, dst_dir_fd=dfd)
        except BaseException:
            _unlink(dfd, tmp)
            raise
        os.fsync(dfd)


def remove(state_dir, workspace_id) -> bool:
    """Delete only known files, leaving unrelated host data intact."""
    with _open_dir(github_dir(state_dir, workspace_id), os.getuid()) as dfd:
        if dfd is None:
            return False
        existed = False
        for name in (TOKEN_FILE, *CACHE_FILES):
            removed = _unlink(dfd, name)
            existed |= removed and name == TOKEN_FILE
        return existed
