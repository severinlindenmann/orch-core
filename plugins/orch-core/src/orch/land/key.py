"""Landing's deploy key, its host-key pin and the pinned executables (#596 task 4, invariants I6 and I8).

* **The key (I8).** One ed25519 deploy key per workspace, generated here and kept in a private directory of the host
  state (``<state>/land/<workspace>/``) under the custody rules the host's other secret files use
  (:func:`orch.host.custody.ensure_private_dir`: 0700, ours, every ancestor checked; the key file ``O_NOFOLLOW``,
  regular, ours, no group/other bit, one link). Only the **public** half ever leaves (:meth:`KeyStore.public_key`).
  Nothing here reads the private key into a value that is returned, logged, put in an environment or in an error: the
  only consumer is ``ssh -i <path>``, started by ``land_push``/``land_fetch`` (:class:`orch.gitsafe.NetSpec`). The
  key is not in the environment of a check process: :mod:`orch.land.sandbox` builds that environment from scratch.
  Every error here carries a fixed ``code`` and a fixed ``reason``, never a path of the key's content, and is raised
  ``from None`` (an exception chain would carry the library's own text).
* **The pin (never TOFU later).** The host key of a remote is written once, at login (:meth:`KeyStore.pin_host_key`,
  from ``ssh-keyscan`` output the person confirmed by fingerprint), and ssh runs with ``StrictHostKeyChecking=yes``
  against that file only. A missing pin is a refusal (``land.no_pin``); replacing one is an explicit ``replace=True``.
* **Pinned executables (Opus F2).** ``git`` and ``ssh`` are fixed absolute paths in system directories (``/usr/bin``)
  whose file and every directory above it are root-owned and not writable by others (a Homebrew ``ssh`` in a
  user-writable directory could be swapped for a wrapper that sends the key anywhere). Not found on ``PATH``.

Same-UID residual (tier A, stated in the grant review): a process of the same user can read the key file, exactly as
it can use the user's own ssh-agent today. #287 (agents as their own user) closes it.
"""

from __future__ import annotations

import base64
import binascii
import contextlib
import hashlib
import os
import re
import secrets
import stat
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from orch import gitsafe
from orch.host import custody
from orch.host._unix import UnsafePath, check_acl
from orch.host.errors import HostError

__all__ = ["KeyStore", "LandKeyError", "fingerprint", "pinned_exe", "remote_host", "scrub", "trusted_exe"]

#: the only places ``git`` and ``ssh`` may come from (Linux and macOS both have them here)
EXE_CANDIDATES: dict[str, tuple[str, ...]] = {"git": ("/usr/bin/git",), "ssh": ("/usr/bin/ssh",)}
COMMENT = "orch-land"
_MAX_PIN_LINES = 8
_KEY_TYPES = frozenset({
    "ssh-ed25519", "ecdsa-sha2-nistp256", "ecdsa-sha2-nistp384", "ecdsa-sha2-nistp521", "ssh-rsa",
})  # fmt: skip
_PRIVATE_BLOCK = re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----.*?(-----END [A-Z0-9 ]*PRIVATE KEY-----|\Z)", re.S)
_BASE64_RUN = re.compile(r"[A-Za-z0-9+/=]{200,}")
_FLAGS_FILE = os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NONBLOCK", 0)


class LandKeyError(Exception):
    """A refusal with a catalog-style ``code`` and a fixed, plain ``reason`` (no path, no key, no library text)."""

    def __init__(self, code: str, reason: str) -> None:
        super().__init__(f"{code}: {reason}")
        self.code = code
        self.reason = reason


def scrub(text: str) -> str:
    """``text`` with every private-key block and every long base64 run replaced (for any text that came from a process
    or an exception and is about to be shown or logged). Defence in depth: nothing here puts key text in a message."""
    return _BASE64_RUN.sub("[redacted]", _PRIVATE_BLOCK.sub("[redacted]", text))


# -- pinned executables ------------------------------------------------------------------------------------------------


def _acl_ok(path: str | os.PathLike[str], trusted: set[int], *, empty: bool) -> bool:
    """Whether the inode has no extended ACL that grants another principal anything (``empty``: no ACL at all). Unix
    mode bits alone miss macOS ACLs and Linux POSIX ACLs (Codex on #762); unsupported inspection fails closed."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NONBLOCK", 0))
    except OSError:
        return False
    try:
        check_acl(fd, trusted, empty=empty)
        return True
    except (UnsafePath, OSError):
        return False
    finally:
        os.close(fd)


def _chain_ok(path: str) -> bool:
    for p in (Path(path), *Path(path).parents):
        st = os.stat(p)
        if st.st_uid != 0 or st.st_mode & 0o022 or not _acl_ok(p, {0}, empty=False):
            return False
    return True


def trusted_exe(path: str) -> bool:
    """A regular executable file, root-owned and not group/other writable, with every directory above it (as named
    and as resolved) root-owned and not writable by others. Tests replace this one function (and
    :data:`EXE_CANDIDATES`) to place a fake ``ssh`` in a temporary directory."""
    try:
        real = os.path.realpath(path)
        if not stat.S_ISREG(os.stat(real).st_mode) or not os.access(real, os.X_OK):
            return False
        return _chain_ok(real) and _chain_ok(os.path.dirname(path))
    except OSError:
        return False


def pinned_exe(name: str) -> str:
    """The absolute path of ``git`` or ``ssh``: the first candidate of :data:`EXE_CANDIDATES` that
    :func:`trusted_exe` accepts. Never looked up on ``PATH``; a user-writable one (``/opt/homebrew/bin``) is refused."""
    for cand in EXE_CANDIDATES.get(name, ()):
        if os.path.isfile(cand) and trusted_exe(cand):
            return cand
    raise LandKeyError("land.untrusted_exe", f"{name} is not a root-owned system program")


# -- the remote --------------------------------------------------------------------------------------------------------

_REMOTE = re.compile(r"ssh://[^@/]+@(?P<host>[A-Za-z0-9][A-Za-z0-9.-]*)(?::(?P<port>[0-9]+))?/")


def remote_host(remote: str) -> str:
    """The ``known_hosts`` host field of ``remote`` (an ``ssh://`` URL gitsafe accepts): ``host``, or ``[host]:port``
    for a port other than 22. Raises :class:`LandKeyError` for any other shape."""
    try:
        gitsafe._check("url", remote)  # noqa: SLF001 - the one validator of remotes
    except gitsafe.Unsafe:
        raise LandKeyError("land.bad_remote", "the remote must be an ssh:// address") from None
    m = _REMOTE.match(remote)
    if m is None:
        raise LandKeyError("land.bad_remote", "the remote must be an ssh:// address")
    host, port = m.group("host").lower(), m.group("port")
    return host if port in (None, "22") else f"[{host}]:{int(port)}"


# -- host key lines ----------------------------------------------------------------------------------------------------


def _parse_key_line(line: str, host: str) -> tuple[str, str]:
    """``(type, base64)`` of one ``known_hosts`` line for exactly ``host`` (no wildcard, hash, marker or option)."""
    parts = line.split()
    if len(parts) < 3 or parts[0] != host or parts[1] not in _KEY_TYPES:
        raise LandKeyError("land.bad_host_key", "a host key line is not for this remote or not a plain host key")
    try:
        blob = base64.b64decode(parts[2], validate=True)
    except (binascii.Error, ValueError):
        raise LandKeyError("land.bad_host_key", "a host key is not valid base64") from None
    n = int.from_bytes(blob[:4], "big")
    if len(blob) < 8 or blob[4 : 4 + n] != parts[1].encode():
        raise LandKeyError("land.bad_host_key", "a host key does not match its type")
    return parts[1], parts[2]


def fingerprint(key_b64: str) -> str:
    """``SHA256:<base64>`` of a host key blob, as ssh shows it (what the person confirms at login)."""
    digest = hashlib.sha256(base64.b64decode(key_b64, validate=True)).digest()
    return "SHA256:" + base64.b64encode(digest).decode().rstrip("=")


# -- the store ---------------------------------------------------------------------------------------------------------


def _unsafe() -> LandKeyError:
    return LandKeyError("land.key_unsafe", "the landing key directory is not private to this user")


class KeyStore:
    """The landing key and pins of one workspace under ``<state_dir>/land/<hash of the workspace id>/``."""

    def __init__(self, state_dir: str | os.PathLike[str], workspace_id: str, *, host_uid: int | None = None) -> None:
        if not isinstance(workspace_id, str) or not workspace_id or "\0" in workspace_id:
            raise LandKeyError("land.bad_input", "the workspace id is not valid")
        self.host_uid = os.geteuid() if host_uid is None else host_uid
        self._state = Path(state_dir)
        self._ws = hashlib.sha256(workspace_id.encode("utf-8", "surrogatepass")).hexdigest()[:32]

    def __repr__(self) -> str:  # never anything but the type: not a path, not the id
        return "<KeyStore>"

    __str__ = __repr__

    # -- directories
    def _dir(self, *, create: bool) -> Path:
        try:
            base = custody.ensure_private_dir(self._state / "land", self.host_uid, create=create)
            out = custody.ensure_private_dir(base / self._ws, self.host_uid, create=create)
            if not (_acl_ok(base, {self.host_uid}, empty=True) and _acl_ok(out, {self.host_uid}, empty=True)):
                raise _unsafe()
            return out
        except HostError as e:
            if e.code == "not_found":
                raise LandKeyError("land.no_key", "this workspace has no landing key yet") from None
            raise _unsafe() from None
        except OSError:
            raise _unsafe() from None

    def _pins_dir(self, *, create: bool) -> Path:
        base = self._dir(create=create)
        try:
            out = custody.ensure_private_dir(base / "known_hosts", self.host_uid, create=create)
            if not _acl_ok(out, {self.host_uid}, empty=True):
                raise _unsafe()
            return out
        except HostError:
            raise _unsafe() from None

    @property
    def _key_path(self) -> Path:
        return self._state / "land" / self._ws / "key"

    def _secret_file_ok(self, path: Path) -> None:
        try:
            fd = os.open(path, _FLAGS_FILE)
        except FileNotFoundError:
            raise LandKeyError("land.no_key", "this workspace has no landing key yet") from None
        except OSError:
            raise _unsafe() from None
        try:
            st = os.fstat(fd)
            try:
                check_acl(fd, {self.host_uid}, empty=True)  # an ACL could grant a read the mode bits do not show
            except (UnsafePath, OSError):
                raise LandKeyError(
                    "land.key_unsafe", "the landing key file is not a private file of this user"
                ) from None
        finally:
            os.close(fd)
        if (
            not stat.S_ISREG(st.st_mode) or st.st_uid != self.host_uid or stat.S_IMODE(st.st_mode) & 0o177
            or st.st_nlink != 1
        ):  # fmt: skip
            raise LandKeyError("land.key_unsafe", "the landing key file is not a private file of this user")

    # -- the key
    def generate(self, *, replace: bool = False) -> str:
        """Create the workspace's deploy key (ed25519) and return its public line. An existing key is
        ``land.key_exists`` unless ``replace`` (a new key: the old one stops working at the remote once the person
        swaps it there)."""
        directory = self._dir(create=True)
        private = Ed25519PrivateKey.generate()
        pem = private.private_bytes(
            serialization.Encoding.PEM, serialization.PrivateFormat.OpenSSH, serialization.NoEncryption()
        )
        final = directory / "key"
        tmp = directory / f"key.{secrets.token_hex(8)}.tmp"
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
        try:
            fd = os.open(tmp, flags, 0o600)
            try:
                os.fchmod(fd, 0o600)
                os.write(fd, pem)
                os.fsync(fd)
            finally:
                os.close(fd)
            if replace:
                os.replace(tmp, final)
            else:
                try:
                    os.link(tmp, final)
                except FileExistsError:
                    raise LandKeyError("land.key_exists", "this workspace already has a landing key") from None
        except OSError:
            raise LandKeyError("land.key_unavailable", "the landing key cannot be written") from None
        finally:
            with contextlib.suppress(OSError):
                os.unlink(tmp)
        return self.public_key()

    def public_key(self) -> str:
        """The public half as one OpenSSH line (``ssh-ed25519 AAAA... orch-land``): what the person adds as a deploy
        key at the remote. Derived from the private file each time (no second copy to drift)."""
        self._dir(create=False)
        path = self._key_path
        self._secret_file_ok(path)
        try:
            with open(path, "rb") as f:  # noqa: PTH123 - mode and owner were checked just above
                loaded = serialization.load_ssh_private_key(f.read(16384), password=None)
            if not isinstance(loaded, Ed25519PrivateKey):
                raise ValueError
            line = loaded.public_key().public_bytes(serialization.Encoding.OpenSSH, serialization.PublicFormat.OpenSSH)
        except Exception:  # noqa: BLE001 - the library's text may quote the file; never let it out
            raise LandKeyError("land.key_unsafe", "the landing key file is not a usable ed25519 key") from None
        return f"{line.decode('ascii')} {COMMENT}"

    # -- the pin
    def pin_host_key(self, remote: str, scan: str, *, replace: bool = False) -> list[str]:
        """Write the host key(s) of ``remote`` from ``ssh-keyscan`` output (``scan``) as its pin, and return their
        fingerprints for the person to confirm. Only lines for exactly this host (a bare name, or ``[host]:port``) of a
        plain key type are accepted; comments are dropped; nothing else is written. An existing pin is
        ``land.pin_exists`` unless ``replace``."""
        host = remote_host(remote)
        lines = [ln.strip() for ln in scan.splitlines() if ln.strip() and not ln.lstrip().startswith("#")]
        if not lines or len(lines) > _MAX_PIN_LINES:
            raise LandKeyError("land.bad_host_key", "expected one to eight host key lines")
        keys = [_parse_key_line(ln, host) for ln in lines]
        body = "".join(f"{host} {t} {b}\n" for t, b in keys).encode("ascii")
        directory = self._pins_dir(create=True)
        final = directory / self._pin_name(host)
        tmp = directory / f"pin.{secrets.token_hex(8)}.tmp"
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
        try:
            fd = os.open(tmp, flags, 0o600)
            try:
                os.fchmod(fd, 0o600)
                os.write(fd, body)
                os.fsync(fd)
            finally:
                os.close(fd)
            if replace:
                os.replace(tmp, final)
            else:
                try:
                    os.link(tmp, final)
                except FileExistsError:
                    raise LandKeyError("land.pin_exists", "this remote already has a pinned host key") from None
        except OSError:
            raise LandKeyError("land.key_unavailable", "the host key pin cannot be written") from None
        finally:
            with contextlib.suppress(OSError):
                os.unlink(tmp)
        return [fingerprint(b) for _t, b in keys]

    @staticmethod
    def _pin_name(host: str) -> str:
        return hashlib.sha256(host.encode()).hexdigest()[:32]

    # -- what a network operation gets
    def net_spec(self, remote: str) -> gitsafe.NetSpec:
        """The :class:`orch.gitsafe.NetSpec` for ``remote``: pinned ``git`` and ``ssh``, this workspace's key file and
        this remote's pin, each checked now (private directories and files of this user). A missing key is
        ``land.no_key``, a missing pin ``land.no_pin`` (never "accept on first use")."""
        host = remote_host(remote)
        git, ssh = pinned_exe("git"), pinned_exe("ssh")
        directory = self._dir(create=False)
        self._secret_file_ok(directory / "key")
        pins = self._pins_dir(create=False) if (directory / "known_hosts").is_dir() else None
        pin = None if pins is None else pins / self._pin_name(host)
        if pin is None or not os.path.lexists(pin):
            raise LandKeyError("land.no_pin", "the host key of this remote has not been pinned")
        self._secret_file_ok(pin)
        try:
            return gitsafe.NetSpec(git=git, ssh=ssh, key=str(directory / "key"), known_hosts=str(pin))
        except gitsafe.Unsafe:
            raise LandKeyError("land.key_unsafe", "the landing key paths are not usable in an ssh command") from None
