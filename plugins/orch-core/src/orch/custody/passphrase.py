"""The ``passphrase`` backend (D65, all OSes, the P1 default): a P-256 signing key stored encrypted under a passphrase.

Key file (JSON, one per key, mode 0600)::

    {"v": 1, "key_id": ..., "pub": b64u(65), "kdf": {"name": "scrypt", "n": 131072, "r": 8, "p": 1, "salt": b64u(16)},
     "nonce": b64u(12), "ct": b64u(32 + 16)}

* **KDF.** scrypt (RFC 7914) through ``cryptography``, ``N = 2^17, r = 8, p = 1`` (128 MiB, the OWASP-recommended
  minimum for scrypt), a fresh 16-byte salt per key, 32-byte output. The parameters are in the file so they can rise
  later. On load they are checked: ``r = 8, p = 1`` and ``N`` a power of two in ``[min_n, 2^20]`` (``min_n``
  defaults to 2^15; only tests lower it), so a swapped file can neither weaken the KDF below the floor nor make the
  host allocate gigabytes.
* **Cipher.** AES-256-GCM, a fresh random nonce, the AAD is ``"orch/v2/custody-file|" || cj(header)`` over
  ``{v, key_id, pub, kdf}``, so the file's public key, id and KDF parameters are authenticated. After decrypting, the
  scalar must reproduce ``pub``.
* **Per signature, never cached** (ticket-format §5.3, §12 O2). :meth:`sign` asks for the passphrase, runs the KDF,
  decrypts the scalar into a ``bytearray``, signs, overwrites the ``bytearray`` and drops every reference. The backend
  object holds no key, no passphrase and no derived key between calls (tested: two signatures run the KDF twice and
  ``vars(backend)`` holds only configuration). Python cannot guarantee that no copy of the scalar survives in
  ``cryptography``'s or the interpreter's memory; this is best effort, stated in the doctor tier line.
* **A prompt or nothing.** The default provider writes the prompt to ``/dev/tty`` and reads the passphrase from it
  with echo off (never stdin/stdout/stderr, which an agent may hold), and raises :class:`NoPrompt` without a
  terminal. Every value in the prompt goes through :func:`shown` (``orch.cli.render.clean_line``: escapes ESC, C0/C1,
  CR, bidi and invisible characters, one line, 80 characters), under fixed labels, one per line; the hash shown is
  computed from the signing bytes by the backend; tests inject a callback. There is no environment
  variable, no cache file and no
  command-line form of the passphrase.
* **Minimum length.** 8 characters when a key is created (portable-custody §7 leaves the minimum open; the owner can
  raise it). An existing file never checks length.
"""

from __future__ import annotations

import json
import os
import secrets
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt

from orch import canon, crypto

from . import files
from .base import PERSON_LABELS as _PERSON_LABELS
from .base import (
    CustodyError,
    KeyNotFound,
    NoPrompt,
    WrongPassphrase,
    check_key_id,
    label_allowed,
)

__all__ = [
    "DEFAULT_KDF",
    "MIN_PASSPHRASE_CHARS",
    "KdfParams",
    "PassphraseBackend",
    "PassphraseRequest",
    "render_prompt",
    "shown",
    "tty_passphrase_provider",
]

FILE_SUFFIX = ".key.json"
AAD_LABEL = b"orch/v2/custody-file|"
MIN_PASSPHRASE_CHARS = 8
MAX_N = 2**20


@dataclass(frozen=True)
class KdfParams:
    n: int = 2**17
    r: int = 8
    p: int = 1


DEFAULT_KDF = KdfParams()


@dataclass(frozen=True)
class PassphraseRequest:
    """What a passphrase prompt shows (D41: the prompt names the action and the hash)."""

    kind: str  # "create" or "unlock"
    key_id: str
    action: str
    digest: str  # first 32 hex characters of sha256(payload), "" for create


PassphraseProvider = Callable[[PassphraseRequest], str]


MAX_SHOWN = 80
_HEX = frozenset("0123456789abcdef")


def shown(text: object, limit: int = MAX_SHOWN) -> str:
    """The one escaping step for every value in the prompt: ``orch.cli.render.clean_line`` (invisible, bidi, C0/C1 and
    ESC become visible markers; one line), then cut to ``limit`` characters with a visible ellipsis."""
    from orch.cli.render import clean_line  # lazy: cli imports ops, which may import custody later

    out = clean_line(text if isinstance(text, str) else repr(text))
    return out if len(out) <= limit else out[: limit - 1] + "\u2026"


def render_prompt(request: PassphraseRequest) -> str:
    """The text a person reads before typing a passphrase. Fixed labels the input can't contain (values are escaped
    to one line and can't hold a newline), one value per line, and the hash is the digest of the bytes about to be
    signed, which the backend computes (not caller text)."""
    digest = request.digest if request.digest and set(request.digest) <= _HEX else shown(request.digest, 64)
    lines = ["", "=== orch: passphrase ==="]
    if request.kind == "create":
        lines += [f"key:    {shown(request.key_id, 64)}", "action: create this key"]
    else:
        lines += [f"key:    {shown(request.key_id, 64)}", f"action: {shown(request.action)}", f"sha256: {digest}"]
    return "\n".join(lines) + "\n"


def _open_tty():
    """The controlling terminal as ``(read_fd, write_fd, close)``, or :class:`NoPrompt`. Never stdin/stdout/stderr."""
    if os.name == "nt":  # pragma: no cover - exercised on Windows CI
        try:
            r, w = os.open("CONIN$", os.O_RDWR), os.open("CONOUT$", os.O_RDWR)
        except OSError:
            raise NoPrompt("no console: human-only signatures need a real terminal (D65)") from None
    else:
        try:
            r = w = os.open("/dev/tty", os.O_RDWR | os.O_NOCTTY)
        except OSError:
            raise NoPrompt("no controlling terminal: human-only signatures need a real terminal (D65)") from None
        if not os.isatty(r):
            os.close(r)
            raise NoPrompt("/dev/tty is not a terminal")

    def close() -> None:
        os.close(r)
        if w != r:
            os.close(w)

    return r, w, close


def _read_secret(r: int, w: int, prompt: str) -> str:
    """Write ``prompt`` to the terminal and read one line with echo off."""
    os.write(w, prompt.encode("utf-8", "replace"))
    if os.name == "nt":  # pragma: no cover
        import msvcrt

        chars: list[str] = []
        while True:
            ch = msvcrt.getwch()
            if ch in ("\r", "\n"):
                break
            if ch == "\x03":
                raise KeyboardInterrupt
            if ch == "\b":
                chars = chars[:-1]
            else:
                chars.append(ch)
        os.write(w, b"\r\n")
        return "".join(chars)
    import termios

    old = termios.tcgetattr(r)
    new = old[:]
    new[3] &= ~termios.ECHO
    termios.tcsetattr(r, termios.TCSAFLUSH, new)
    try:
        buf = bytearray()
        while True:
            c = os.read(r, 1)
            if not c or c == b"\n":
                break
            buf += c
    finally:
        termios.tcsetattr(r, termios.TCSAFLUSH, old)
        os.write(w, b"\n")
    return buf.decode("utf-8", "replace").rstrip("\r")


def tty_passphrase_provider(request: PassphraseRequest) -> str:
    """Ask on the person's own terminal: the prompt goes to ``/dev/tty`` (``CONOUT$`` on Windows) and the passphrase is
    read from it with echo off, never through stdin, stdout or stderr, which an agent may control. Refuses
    (:class:`NoPrompt`) without a terminal, so a human-only signature can never run under an agent's pipe (D65)."""
    r, w, close = _open_tty()
    try:
        header = render_prompt(request)
        first = _read_secret(r, w, header + "Passphrase: ")
        if request.kind == "create" and _read_secret(r, w, "Repeat passphrase: ") != first:
            raise CustodyError("passphrases differ")
        return first
    finally:
        close()


def _derive(passphrase: str, salt: bytes, params: KdfParams) -> bytes:
    """The one place the KDF runs (tests count calls through this name)."""
    return Scrypt(salt=salt, length=32, n=params.n, r=params.r, p=params.p).derive(passphrase.encode("utf-8"))


def _zeroise(buf: bytearray) -> None:
    for i in range(len(buf)):
        buf[i] = 0


class PassphraseBackend:
    name = "passphrase"
    auth = "passphrase"
    person_capable = True

    def __init__(
        self,
        directory: str | Path,
        *,
        passphrase_provider: PassphraseProvider | None = None,
        kdf: KdfParams = DEFAULT_KDF,
        min_n: int = 2**15,
    ) -> None:
        if not (min_n <= kdf.n <= MAX_N) or kdf.n & (kdf.n - 1) or kdf.r != 8 or kdf.p != 1:
            raise CustodyError("unacceptable scrypt parameters")
        self._dir = files.private_dir(directory)
        self._provider = passphrase_provider or tty_passphrase_provider
        self._kdf = kdf
        self._min_n = min_n

    # -- file format --------------------------------------------------------------------------------------------------

    @staticmethod
    def _aad(header: dict) -> bytes:
        return AAD_LABEL + canon.cj_checked(header)

    def _path(self, key_id: str) -> Path:
        return files.key_path(self._dir, key_id, FILE_SUFFIX)

    def _read(self, key_id: str) -> tuple[dict, dict, KdfParams, bytes, bytes, bytes]:
        path = self._path(key_id)
        try:
            doc = json.loads(path.read_bytes())
            if set(doc) != {"v", "key_id", "pub", "kdf", "nonce", "ct"} or doc["v"] != 1 or doc["key_id"] != key_id:
                raise ValueError("shape")
            kdf = doc["kdf"]
            if set(kdf) != {"name", "n", "r", "p", "salt"} or kdf["name"] != "scrypt":
                raise ValueError("kdf")
            params = KdfParams(kdf["n"], kdf["r"], kdf["p"])
            if (
                type(params.n) is not int
                or type(params.r) is not int
                or type(params.p) is not int
                or params.n & (params.n - 1)
                or not self._min_n <= params.n <= MAX_N
                or params.r != 8
                or params.p != 1
            ):
                raise ValueError("kdf parameters outside the accepted range")
            salt, nonce = crypto.unb64u(kdf["salt"], 16), crypto.unb64u(doc["nonce"], 12)
            crypto.validate_public_key(crypto.unb64u(doc["pub"], crypto.PUB_LEN))
            ct = crypto.unb64u(doc["ct"], 48)
        except FileNotFoundError:
            raise KeyNotFound(key_id) from None
        except (ValueError, KeyError, TypeError, crypto.CryptoError) as e:
            raise CustodyError(f"corrupt key file for {key_id!r}: {e}") from None
        header = {"v": 1, "key_id": key_id, "pub": doc["pub"], "kdf": kdf}
        return doc, header, params, salt, nonce, ct

    # -- interface ----------------------------------------------------------------------------------------------------

    def create(self, key_id: str, *, secret: bytes | None = None) -> bytes:
        check_key_id(key_id)
        path = self._path(key_id)
        if path.exists():
            raise files.KeyExists(key_id)
        if secret is None:
            key = crypto.generate_private_key()
        else:
            if type(secret) is not bytes or len(secret) != 32:
                raise CustodyError("secret must be the 32-byte scalar")
            key = crypto.private_key_from_scalar(int.from_bytes(secret, "big"))
        pub = crypto.public_bytes(key)
        passphrase = self._provider(PassphraseRequest("create", key_id, "create the key", ""))
        if not isinstance(passphrase, str) or len(passphrase) < MIN_PASSPHRASE_CHARS:
            raise CustodyError(f"passphrase needs at least {MIN_PASSPHRASE_CHARS} characters")
        salt, nonce = secrets.token_bytes(16), secrets.token_bytes(12)
        kdf = {"name": "scrypt", "n": self._kdf.n, "r": self._kdf.r, "p": self._kdf.p, "salt": crypto.b64u(salt)}
        header = {"v": 1, "key_id": key_id, "pub": crypto.b64u(pub), "kdf": kdf}
        scalar = bytearray(crypto.private_scalar(key))
        derived = bytearray(_derive(passphrase, salt, self._kdf))
        try:
            ct = crypto.aes_gcm_seal(bytes(derived), nonce, bytes(scalar), self._aad(header))
        finally:
            _zeroise(scalar)
            _zeroise(derived)
        doc = {**header, "nonce": crypto.b64u(nonce), "ct": crypto.b64u(ct)}
        files.write_new(path, json.dumps(doc, sort_keys=True, separators=(",", ":")).encode("ascii"))
        return pub

    def public_key(self, key_id: str) -> bytes:
        check_key_id(key_id)
        doc, *_ = self._read(key_id)
        return crypto.unb64u(doc["pub"], crypto.PUB_LEN)

    def sign(self, key_id: str, payload: bytes, *, action: str) -> bytes:
        check_key_id(key_id)
        if not label_allowed(payload, _PERSON_LABELS):
            raise CustodyError("this key signs person-tier payloads only (unknown or host-only signature label)")
        doc, header, params, salt, nonce, ct = self._read(key_id)
        digest = crypto.sha256(payload).hex()[:32]
        passphrase = self._provider(PassphraseRequest("unlock", key_id, shown(action), digest))
        if not isinstance(passphrase, str) or not passphrase:
            raise WrongPassphrase("empty passphrase")
        derived = bytearray(_derive(passphrase, salt, params))
        scalar = bytearray()
        try:
            try:
                scalar = bytearray(crypto.aes_gcm_open(bytes(derived), nonce, ct, self._aad(header)))
            except InvalidTag:
                raise WrongPassphrase("wrong passphrase") from None
            key = crypto.private_key_from_scalar(int.from_bytes(scalar, "big"))
            if crypto.public_bytes(key) != crypto.unb64u(doc["pub"], crypto.PUB_LEN):
                raise CustodyError("key file does not match its public key")
            return crypto.sign(key, payload)
        finally:
            _zeroise(derived)
            _zeroise(scalar)
            key = None  # noqa: F841

    def presence(self) -> str:
        return "passphrase"

    def exists(self, key_id: str) -> bool:
        return self._path(key_id).exists()

    def delete(self, key_id: str) -> None:
        files.delete_file(self._path(key_id))
