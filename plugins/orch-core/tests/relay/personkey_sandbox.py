"""The dev sandbox's person-key source (orch-v2-host 11A.15.11 "The sandbox identity"; 11A.15.10 item 10).

**Test tree only, never packaged.** The scripted dev path (``scripts/dev-pair``) runs a throwaway person whose
recovery code lives in the sandbox (``<root>/secrets/recovery-code``, 0600). Production code never reads a stored
code or key (owner decision Q1 = B, and the narrowing rule of 11A.15.11 N8a: nothing in the environment or on disk can
select a weaker source), so this source is here, beside ``tests/relay/owner.py``'s family, and it refuses to run
unless it finds the sandbox's manifest ``<root>/.orch-dev-sandbox`` naming that very root.

It implements :class:`orch.custody.personkey.PersonKeySource` with the production label policy
(:func:`orch.custody.personkey.check_payload`) and the production label-key derivation, so the dev path exercises
the same contract as :class:`~orch.custody.personkey.RecoveryCodeSource`; only where the code comes from differs.

**Manifest check (minimal, for orch-29).** ``tests/relay/owner.py`` is not on develop yet, so this module carries its
own check: ``<root>`` is a real directory (``lstat``, no symbolic link) owned by this user and not group- or
world-accessible; ``<root>/.orch-dev-sandbox`` is a regular file opened with ``O_NOFOLLOW`` through the root's
directory descriptor, owned by this user, mode 0600, JSON with a non-empty ``id`` and ``root: {st_dev, st_ino}``
equal to the root's. When ``owner.py`` and ``dev-pair`` land, they should share :func:`check_manifest` (or replace it
with theirs and keep these tests). **The marker is accident prevention, not containment** (11A.15.11).
"""

from __future__ import annotations

import json
import os
import secrets
import stat
from collections.abc import Callable
from pathlib import Path
from typing import Any

from orch import crypto
from orch.crypto.labels import L
from orch.custody import personkey
from orch.custody.base import CustodyError
from orch.identity import recovery

MANIFEST = ".orch-dev-sandbox"
SECRETS = "secrets"
CODE_FILE = "recovery-code"
_MAX = 4096


class SandboxRefused(CustodyError):
    code = "sandbox.refused"


def _dir_fd(root: Path) -> tuple[int, os.stat_result]:
    try:
        st = os.lstat(root)
    except OSError:
        raise SandboxRefused("no sandbox root") from None
    if not stat.S_ISDIR(st.st_mode) or st.st_uid != os.getuid() or st.st_mode & 0o077:
        raise SandboxRefused("the sandbox root is not a private directory of this user")
    fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    got = os.fstat(fd)
    if (got.st_dev, got.st_ino) != (st.st_dev, st.st_ino):
        os.close(fd)
        raise SandboxRefused("the sandbox root changed while it was opened")
    return fd, st


def _read_private(dir_fd: int, name: str) -> bytearray:
    """A 0600 regular file of this user, opened without following a symbolic link, into a bytearray."""
    try:
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=dir_fd)
    except OSError:
        raise SandboxRefused(f"no {name} in the sandbox") from None
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_uid != os.getuid() or stat.S_IMODE(st.st_mode) != 0o600:
            raise SandboxRefused(f"{name} is not a 0600 file of this user")
        buf = bytearray()
        while len(buf) <= _MAX:
            chunk = os.read(fd, 1024)
            if not chunk:
                break
            buf += chunk
        if len(buf) > _MAX:
            personkey._zeroise(buf)
            raise SandboxRefused(f"{name} is too large")
        return buf
    finally:
        os.close(fd)


def check_manifest(root: str | os.PathLike[str]) -> dict[str, Any]:
    """The sandbox manifest of ``root``, or :class:`SandboxRefused`."""
    fd, st = _dir_fd(Path(root))
    try:
        raw = _read_private(fd, MANIFEST)
    finally:
        os.close(fd)
    try:
        doc = json.loads(bytes(raw))
    except ValueError:
        raise SandboxRefused("the sandbox manifest is not JSON") from None
    pin = doc.get("root") if isinstance(doc, dict) else None
    if (
        not isinstance(doc, dict)
        or type(doc.get("id")) is not str
        or not doc["id"]
        or not isinstance(pin, dict)
        or (pin.get("st_dev"), pin.get("st_ino")) != (st.st_dev, st.st_ino)
    ):
        raise SandboxRefused("the sandbox manifest does not name this root")
    return doc


def make_sandbox(root: str | os.PathLike[str], code: str) -> dict[str, Any]:
    """Test helper (and the shape ``dev-pair up`` writes): create ``root`` exclusively (0700), its manifest and
    ``secrets/recovery-code`` (0600). Refuses an existing path."""
    root = Path(root)
    os.mkdir(root, 0o700)
    st = os.lstat(root)
    doc = {"id": secrets.token_hex(16), "root": {"st_dev": st.st_dev, "st_ino": st.st_ino}}
    _write(root / MANIFEST, json.dumps(doc).encode())
    os.mkdir(root / SECRETS, 0o700)
    _write(root / SECRETS / CODE_FILE, code.encode())
    return doc


def _write(path: Path, data: bytes) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        os.write(fd, data)
    finally:
        os.close(fd)


class SandboxPersonKey:
    """The sandbox person's key for one command, as :class:`~orch.custody.personkey.RecoveryCodeSource` but read
    from ``<root>/secrets/recovery-code`` after the manifest check. ``_kdf_n`` is the low-N seam for fast tests."""

    __slots__ = ("_kdf_n", "_live", "_person_id", "_pk_pub", "_root", "_state")

    def __init__(
        self,
        root: str | os.PathLike[str],
        *,
        person_id: bytes | None = None,
        pk_pub: bytes | None = None,
        _kdf_n: int | None = None,
    ) -> None:
        self._person_id, self._pk_pub = personkey._expected(person_id, pk_pub)
        self._root = Path(root)
        self._kdf_n = _kdf_n
        self._live: list[Any] = []
        self._state = "new"

    @property
    def person_id(self) -> bytes:
        return self._person_id

    @property
    def pk_pub(self) -> bytes:
        if self._pk_pub is None:
            raise personkey.PersonKeyClosed("the public key is known once the code has been read")
        return self._pk_pub

    def __repr__(self) -> str:
        return f"SandboxPersonKey(person=p_{self._person_id.hex()}, state={self._state})"

    def __reduce_ex__(self, protocol: object) -> Any:
        raise TypeError("a person-key source cannot be copied or pickled")

    def __enter__(self) -> SandboxPersonKey:
        if self._state != "new":
            raise personkey.PersonKeyClosed("a sandbox source serves one command")
        self._state = "closed"
        check_manifest(self._root)
        typed, scalar = bytearray(), bytearray()
        opened = False
        try:
            fd, _ = _dir_fd(self._root)
            try:
                sfd = os.open(SECRETS, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            except OSError:
                raise SandboxRefused("no secrets directory in the sandbox") from None
            finally:
                os.close(fd)
            try:
                typed = _read_private(sfd, CODE_FILE)
            finally:
                os.close(sfd)
            try:
                text = recovery.check_recovery_code(bytes(typed).decode("utf-8"))
            except (UnicodeDecodeError, recovery.RecoveryCodeError) as e:
                raise personkey.BadRecoveryCode(getattr(e, "code", "recovery.words")) from None
            n = self._kdf_n
            d = recovery.derive_person_scalar(text) if n is None else recovery._scalar(text, n)
            scalar += d.to_bytes(32, "big")
            key = crypto.private_key_from_scalar(d)
            pub = crypto.public_bytes(key)
            if crypto.person_id(pub) != self._person_id or (self._pk_pub is not None and pub != self._pk_pub):
                raise personkey.PersonMismatch("the sandbox code is not the expected person's")
            self._pk_pub = pub
            self._live[:] = [key, scalar]
            self._state = "open"
            opened = True
            return self
        finally:
            personkey._zeroise(typed)
            if not opened:
                personkey._zeroise(scalar)

    def __exit__(self, *exc: object) -> bool:
        live, self._live = self._live, []
        self._state = "closed"
        for item in live:
            if isinstance(item, bytearray):
                personkey._zeroise(item)
        return False

    def _key(self) -> Any:
        if self._state != "open" or not self._live:
            raise personkey.PersonKeyClosed("the sandbox person key is not unlocked")
        return self._live[0]

    def sign(self, label: str, payload: bytes) -> bytes:
        personkey.check_payload(label, payload)
        return crypto.sign(self._key(), payload)

    def signer(self, label: str) -> Callable[[bytes], bytes]:
        personkey.check_payload(label, L[label] if label in personkey.SIGN_LABELS else None)
        return lambda payload: self.sign(label, payload)

    def derive_label_key(self) -> bytes:
        self._key()
        return crypto.hkdf(bytes(self._live[1]), b"", personkey.LABEL_KEY_INFO)
