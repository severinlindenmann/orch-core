"""Keep credentials opaque so incidental formatting cannot disclose them."""

from __future__ import annotations

import hashlib
import hmac
import os
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


def _directory(path: Path, host_uid: int) -> bool:
    info = custody._lstat_owned(path, host_uid)
    if info is None:
        return False
    if not stat.S_ISDIR(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o700:
        raise custody.unsafe("GitHub custody requires a private directory")
    custody.check_tree(path, host_uid)
    return True


def load(state_dir, workspace_id, host_uid) -> Token:
    """Refuse compromised custody rather than silently repairing a disclosed credential."""
    try:
        directory = github_dir(state_dir, workspace_id)
        if not _directory(directory, host_uid):
            raise NoToken()
        path = directory / TOKEN_FILE
        if custody._lstat_owned(path, host_uid) is None:
            raise NoToken()
        doc = custody.read_json(path, host_uid, limit=4096)
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
    _directory(directory, host_uid)
    custody.ensure_private_dir(directory, host_uid, create=True)
    for name in CACHE_FILES:
        (directory / name).unlink(missing_ok=True)
    custody.write_private(directory / TOKEN_FILE, {"v": 1, "token": token.reveal()})


def remove(state_dir, workspace_id) -> bool:
    """Delete only known files, leaving unrelated host data intact."""
    directory = github_dir(state_dir, workspace_id)
    if not _directory(directory, os.getuid()):
        return False
    existed = False
    for name in (TOKEN_FILE, *CACHE_FILES):
        try:
            (directory / name).unlink()
            existed |= name == TOKEN_FILE
        except FileNotFoundError:
            pass
    return existed
