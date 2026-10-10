"""The ``passphrase`` backend (D65, all OSes, the P1 default): a P-256 signing key stored encrypted under a passphrase.

Key file (JSON, one per key, mode 0600)::

    {"v": 1, "key_id": ..., "role": "person"|"device", "pub": b64u(65),
     "kdf": {"name": "scrypt", "n": 131072, "r": 8, "p": 1, "salt": b64u(16)},
     "nonce": b64u(12), "ct": b64u(32 + 16)}

* **KDF.** scrypt (RFC 7914) through ``cryptography``, ``N = 2^17, r = 8, p = 1`` (128 MiB, the OWASP-recommended
  minimum for scrypt), a fresh 16-byte salt per key, 32-byte output. The parameters are in the file so they can rise
  later. On load they are checked: ``r = 8, p = 1`` and ``N`` a power of two in ``[MIN_N, 2^20]`` (``MIN_N = 2^15``),
  so a swapped file can neither weaken the KDF below the floor nor make the host allocate gigabytes. The floor and the
  default can be lowered only through underscore test seams (``_kdf``, ``_min_n``), which no production path uses.
* **Passphrase text.** NFC-normalised with :func:`orch.canon.nfc` (Unicode 16.0, the same on every OS and Python)
  and encoded as strict UTF-8; a passphrase that is not valid text is refused, never altered (``"replace"`` would
  make distinct byte strings collide). Minimum length and strength: :mod:`orch.custody.strength`, checked on creation.
* **Cipher.** AES-256-GCM, a fresh random nonce, the AAD is ``"orch/v2/custody-file|" || cj(header)`` over
  ``{v, key_id, role, pub, kdf}``, so the file's public key, id, role and KDF parameters are authenticated. After
  decrypting, the scalar must reproduce ``pub``.
* **Per signature, never cached** (ticket-format §5.3, §12 O2). :meth:`sign` asks for the passphrase, runs the KDF,
  decrypts the scalar into a ``bytearray``, signs, overwrites the ``bytearray`` and drops every reference. The backend
  object holds no key, no passphrase and no derived key between calls (tested). Python cannot guarantee that no copy
  of the scalar survives in ``cryptography``'s or the interpreter's memory; this is best effort, stated in the doctor
  tier line.
* **Roles.** A key is ``device`` (dk_sig: person events, decisions, bridge requests) or ``person`` (PK: certificates,
  revocations, delegations, challenges) and signs only that role's labels (:data:`orch.custody.base.ROLE_LABELS`).
* **The prompt** is written to ``/dev/tty`` and the passphrase is read from it with echo off, never through
  stdin/stdout/stderr, which an agent may hold; no controlling terminal means :class:`NoPrompt`. Windows is not
  supported for human signing in P1 and fails closed with :class:`NoPrompt`. What the person reads is derived from
  the signing bytes (:mod:`orch.custody.describe`), under fixed labels, one value per line, each value escaped exactly
  once with :func:`orch.canon.clean` and cut to 120 characters; ``sha256:`` is the first line. Caller text is shown
  last, as a quoted note marked as not signed. Limits (D65): a harness that shares the person's terminal can still
  trigger the prompt; the passphrase typed where an agent can read it is not protected.
* **Files.** Created atomically and exclusively (:func:`orch.custody.files.write_new`), mode 0600; on load the mode
  and owner are checked.
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
from .base import (
    ROLE_LABELS,
    CustodyError,
    KeyNotFound,
    NoPrompt,
    WrongPassphrase,
    check_key_id,
    label_allowed,
)
from .describe import describe_payload
from .strength import MIN_CHARS as MIN_PASSPHRASE_CHARS
from .strength import check_strength

__all__ = [
    "DEFAULT_KDF",
    "MIN_N",
    "MIN_PASSPHRASE_CHARS",
    "KdfParams",
    "PassphraseBackend",
    "PassphraseRequest",
    "render_prompt",
    "tty_passphrase_provider",
]

FILE_SUFFIX = ".key.json"
AAD_LABEL = b"orch/v2/custody-file|"
MIN_N = 2**15
MAX_N = 2**20
MAX_SHOWN = 120
MAX_NAME = 80
ROLES = ("person", "device")


@dataclass(frozen=True)
class KdfParams:
    n: int = 2**17
    r: int = 8
    p: int = 1


DEFAULT_KDF = KdfParams()


@dataclass(frozen=True)
class PassphraseRequest:
    """What a passphrase prompt shows. ``fields`` come from the signing bytes (:func:`describe_payload`); ``action`` is
    the caller's own note, shown last and marked unsigned. Everything is raw here and escaped in
    :func:`render_prompt`."""

    kind: str  # "create" or "unlock"
    key_id: str
    action: str
    digest: str  # first 32 hex characters of sha256(payload), "" for create
    fields: tuple[tuple[str, str], ...] = ()


PassphraseProvider = Callable[[PassphraseRequest], str]


def _value(text: object, limit: int = MAX_SHOWN) -> str:
    """One escaping step for one displayed value: :func:`orch.canon.clean` (ESC, C0/C1, CR, bidi, invisible and
    ``⟨`` become visible markers), newlines made visible too so a value is always one line, cut to ``limit``."""
    out = canon.clean(text if isinstance(text, str) else repr(text)).replace("\n", "⟨U+000A⟩")
    return out if len(out) <= limit else out[: limit - 1] + "…"


MAX_PROMPT_LINES = 160


def _field_lines(name: object, value: object) -> list[str]:
    """One ``name: value`` line, or several ``name#k/n`` lines when the escaped value is longer than one line, so a
    long value is shown in full and never truncated (a hidden tail could carry the decisive part)."""
    text = canon.clean(value if isinstance(value, str) else repr(value)).replace("\n", "\u27e8U+000A\u27e9")
    label = canon.clean(name if isinstance(name, str) else repr(name)).replace("\n", "\u27e8U+000A\u27e9")
    if len(label) > MAX_NAME:  # truncating would let two different keys look the same
        raise CustodyError("refusing to prompt: a field name is too long to show in full")
    chunks = [text[i : i + MAX_SHOWN] for i in range(0, len(text), MAX_SHOWN)] or [""]
    if len(chunks) == 1:
        return [f"{label}: {chunks[0]}"]
    return [f"{label}#{k}/{len(chunks)}: {c}" for k, c in enumerate(chunks, 1)]


def render_prompt(request: PassphraseRequest) -> str:
    """The text a person reads before typing a passphrase: ``sha256:`` first, then fixed ``label: value`` lines, one
    value per line (a value cannot contain a line break), the caller's note last."""
    digest = (
        request.digest
        if request.digest and set(request.digest) <= set("0123456789abcdef")
        else _value(request.digest, 64)
    )
    lines = ["", "=== orch: passphrase ===", f"sha256: {digest}", f"key: {_value(request.key_id, 64)}"]
    if request.kind == "create":
        lines.append("action: create this key")
    else:
        for name, value in request.fields:
            lines += _field_lines(name, value)
    if request.action:
        lines.append(f'note (caller text, not signed): "{_value(request.action)}"')
    if len(lines) > MAX_PROMPT_LINES:
        raise CustodyError("refusing to prompt: too much to show faithfully")
    return "\n".join(lines) + "\n"


def _open_tty():
    """The controlling terminal as ``(read_fd, write_fd, close)``, or :class:`NoPrompt`. Never stdin/stdout/stderr."""
    if os.name == "nt":
        raise NoPrompt("human-only signatures are not supported on Windows in P1 (fail closed)")
    try:
        fd = os.open("/dev/tty", os.O_RDWR | os.O_NOCTTY)
    except OSError:
        raise NoPrompt("no controlling terminal: human-only signatures need a real terminal (D65)") from None
    if not os.isatty(fd):
        os.close(fd)
        raise NoPrompt("/dev/tty is not a terminal")
    return fd, fd, lambda: os.close(fd)


def _decode(buf: bytes | bytearray) -> str:
    """Strict UTF-8: invalid input is refused, never replaced."""
    try:
        return bytes(buf).decode("utf-8")
    except UnicodeDecodeError:
        raise CustodyError("the passphrase is not valid UTF-8") from None


def _read_secret(r: int, w: int, prompt: str) -> str:
    """Write ``prompt`` to the terminal and read one line with echo off."""
    import termios

    os.write(w, prompt.encode("utf-8", "replace"))
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
    if buf.endswith(b"\r"):
        del buf[-1]
    return _decode(buf)


def tty_passphrase_provider(request: PassphraseRequest) -> str:
    """Ask on the person's own terminal (see the module docstring). Refuses with :class:`NoPrompt` without one."""
    r, w, close = _open_tty()
    try:
        first = _read_secret(r, w, render_prompt(request) + "Passphrase: ")
        if request.kind == "create" and _read_secret(r, w, "Repeat passphrase: ") != first:
            raise CustodyError("passphrases differ")
        return first
    finally:
        close()


def _passphrase_bytes(passphrase: object) -> bytes:
    """NFC, then strict UTF-8 (a lone surrogate is refused)."""
    if not isinstance(passphrase, str):
        raise CustodyError("the passphrase must be text")
    try:
        return canon.nfc(passphrase).encode("utf-8")
    except (UnicodeEncodeError, canon.TextError):
        raise CustodyError("the passphrase is not valid text") from None


def _derive(passphrase: bytes, salt: bytes, params: KdfParams) -> bytes:
    """The one place the KDF runs (tests count calls through this name)."""
    return Scrypt(salt=salt, length=32, n=params.n, r=params.r, p=params.p).derive(passphrase)


def _zeroise(buf: bytearray) -> None:
    for i in range(len(buf)):
        buf[i] = 0


def _params_ok(params: KdfParams, floor: int) -> bool:
    return (
        type(params.n) is int
        and type(params.r) is int
        and type(params.p) is int
        and params.n & (params.n - 1) == 0
        and floor <= params.n <= MAX_N
        and params.r == 8
        and params.p == 1
    )


class PassphraseBackend:
    name = "passphrase"
    auth = "passphrase"
    person_capable = True

    def __init__(
        self,
        directory: str | Path,
        *,
        _passphrase_provider: PassphraseProvider | None = None,
        _kdf: KdfParams = DEFAULT_KDF,
        _min_n: int = MIN_N,
    ) -> None:
        """``_``-prefixed arguments are test seams (an injected passphrase callback, a lowered scrypt floor). Production
        code calls ``PassphraseBackend(directory)`` and gets the terminal prompt and the 2^15 floor."""
        if not _params_ok(_kdf, _min_n):
            raise CustodyError("unacceptable scrypt parameters")
        self._dir = files.private_dir(directory)
        self._provider = _passphrase_provider or tty_passphrase_provider
        self._kdf = _kdf
        self._min_n = _min_n

    # -- file format --------------------------------------------------------------------------------------------------

    @staticmethod
    def _aad(header: dict) -> bytes:
        return AAD_LABEL + canon.cj_checked(header)

    def _path(self, key_id: str) -> Path:
        return files.key_path(self._dir, key_id, FILE_SUFFIX)

    def _read(self, key_id: str):
        path = self._path(key_id)
        files.check_private(path)
        try:
            doc = json.loads(path.read_bytes())
            if (
                set(doc) != {"v", "key_id", "role", "pub", "kdf", "nonce", "ct"}
                or doc["v"] != 1
                or doc["key_id"] != key_id
                or doc["role"] not in ROLES
            ):
                raise ValueError("shape")
            kdf = doc["kdf"]
            if set(kdf) != {"name", "n", "r", "p", "salt"} or kdf["name"] != "scrypt":
                raise ValueError("kdf")
            params = KdfParams(kdf["n"], kdf["r"], kdf["p"])
            if not _params_ok(params, self._min_n):
                raise ValueError("kdf parameters outside the accepted range")
            salt, nonce = crypto.unb64u(kdf["salt"], 16), crypto.unb64u(doc["nonce"], 12)
            crypto.validate_public_key(crypto.unb64u(doc["pub"], crypto.PUB_LEN))
            ct = crypto.unb64u(doc["ct"], 48)
        except FileNotFoundError:
            raise KeyNotFound(key_id) from None
        except (ValueError, KeyError, TypeError, crypto.CryptoError) as e:
            raise CustodyError(f"corrupt key file for {key_id!r}: {e}") from None
        header = {"v": 1, "key_id": key_id, "role": doc["role"], "pub": doc["pub"], "kdf": kdf}
        return doc, header, params, salt, nonce, ct

    # -- interface ----------------------------------------------------------------------------------------------------

    def create(
        self, key_id: str, *, secret: bytes | None = None, role: str = "device", passphrase: str | None = None
    ) -> bytes:
        """Create the key. ``passphrase`` is for a caller that has already asked the person itself on the terminal
        (``orch init`` offers a generated one); without it the backend's own prompt asks (twice). Either way the text
        must pass :func:`orch.custody.strength.check_strength`."""
        check_key_id(key_id)
        if role not in ROLES:
            raise CustodyError(f"the passphrase backend holds person and device keys, not {role!r}")
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
        if passphrase is None:
            passphrase = self._provider(PassphraseRequest("create", key_id, "", ""))
        pw = _passphrase_bytes(passphrase)
        check_strength(passphrase)
        salt, nonce = secrets.token_bytes(16), secrets.token_bytes(12)
        kdf = {"name": "scrypt", "n": self._kdf.n, "r": self._kdf.r, "p": self._kdf.p, "salt": crypto.b64u(salt)}
        header = {"v": 1, "key_id": key_id, "role": role, "pub": crypto.b64u(pub), "kdf": kdf}
        scalar = bytearray(crypto.private_scalar(key))
        derived = bytearray(_derive(pw, salt, self._kdf))
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
        doc, header, params, salt, nonce, ct = self._read(key_id)
        if not label_allowed(payload, ROLE_LABELS[doc["role"]]):
            raise CustodyError(
                f"a {doc['role']} key does not sign this payload (unknown label or another role's label)"
            )
        shown_fields = tuple(describe_payload(payload))  # fail closed: refuses before any prompt
        request = PassphraseRequest(
            "unlock",
            key_id,
            action if isinstance(action, str) else "",
            crypto.sha256(payload).hex()[:32],
            shown_fields,
        )
        pw = _passphrase_bytes(self._provider(request))
        if not pw:
            raise WrongPassphrase("empty passphrase")
        derived = bytearray(_derive(pw, salt, params))
        scalar = bytearray()
        key = None
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
        return self.auth or "none"

    def exists(self, key_id: str) -> bool:
        return self._path(key_id).exists()

    def delete(self, key_id: str) -> None:
        files.delete_file(self._path(key_id))
