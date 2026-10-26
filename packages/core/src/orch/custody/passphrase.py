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
  tier line. The one exception is :meth:`unlocked`: one passphrase, shown with a digest of the whole batch, for the
  duration of one ``orch import v1`` (ticket-format section 14).
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

import contextlib
import json
import os
import secrets
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt

from orch import canon, crypto

from . import files
from .base import (
    KX_PURPOSE_RULES,
    KX_PURPOSES,
    PRIMARY_SIG_LABELS,
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
    "CustodyChangeBackend",
    "PassphraseBackend",
    "PassphraseRequest",
    "PrimarySession",
    "render_prompt",
    "tty_passphrase_provider",
]

FILE_SUFFIX = ".key.json"
AAD_LABEL = b"orch/v2/custody-file|"
MIN_N = 2**15
MAX_N = 2**20
MAX_SHOWN = 120
MAX_NAME = 80
ROLES = ("person", "device", "primary", "kx")
_FIELDS = frozenset({"v", "key_id", "role", "pub", "kdf", "nonce", "ct"})
_KX_FIELDS = _FIELDS | {"purposes", "recipient"}


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
    if request.kind == "batch":
        lines = ["", "=== orch: passphrase for a batch of signatures ===", f"plan digest: {digest}"]
        lines.append(f"key: {_value(request.key_id, 64)}")
        for name, value in request.fields:
            lines += _field_lines(name, value)
        lines.append("this one passphrase signs every event of the plan you just read, and nothing else")
        if request.action:
            lines.append(f'note (caller text, not signed): "{_value(request.action)}"')
        return "\n".join(lines) + "\n"
    if request.kind == "unseal":
        lines = ["", "=== orch: passphrase to open one sealed item ===", f"sha256: {digest}"]
        lines.append(f"key: {_value(request.key_id, 64)}")
        for name, value in request.fields:
            lines += _field_lines(name, value)
        lines.append("this opens one sealed item addressed to this key; nothing is signed")
        return "\n".join(lines) + "\n"
    if request.kind == "primary":
        lines = ["", "=== orch: passphrase for your primary key, for this one command ==="]
        lines.append(f"key: {_value(request.key_id, 64)}")
        for name, value in request.fields:
            lines += _field_lines(name, value)
        lines.append(
            "until this command ends, it may log in to your relay as your primary and open cert requests"
            " addressed to it; it signs nothing else"
        )
        if request.action:
            lines.append(f'note (caller text, not signed): "{_value(request.action)}"')
        return "\n".join(lines) + "\n"
    if request.kind == "convert":
        lines = ["", "=== orch: passphrase to change this key's custody ===", f"key: {_value(request.key_id, 64)}"]
        for name, value in request.fields:
            lines += _field_lines(name, value)
        lines.append("this unlocks the key once to move it; nothing is signed")
        return "\n".join(lines) + "\n"
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


def _harden() -> Callable[[], None]:
    """While a key is unlocked for a batch: no core dump (``RLIMIT_CORE`` 0), and no debugger attach (Linux
    ``PR_SET_DUMPABLE`` 0, macOS ``PT_DENY_ATTACH``, which cannot be undone for the process). Best effort, never an
    error. Returns what puts the reversible parts back."""
    undo: list[Callable[[], None]] = []
    try:
        import resource

        old = resource.getrlimit(resource.RLIMIT_CORE)
        resource.setrlimit(resource.RLIMIT_CORE, (0, old[1]))
        undo.append(lambda: resource.setrlimit(resource.RLIMIT_CORE, old))
    except (ImportError, OSError, ValueError):
        pass
    try:
        import ctypes
        import sys

        libc = ctypes.CDLL(None, use_errno=True)
        if sys.platform == "darwin":
            libc.ptrace(31, 0, 0, 0)  # PT_DENY_ATTACH
        elif sys.platform.startswith("linux"):
            was = libc.prctl(3, 0, 0, 0, 0)  # PR_GET_DUMPABLE
            libc.prctl(4, 0, 0, 0, 0)  # PR_SET_DUMPABLE
            if was in (0, 1, 2):
                undo.append(lambda: libc.prctl(4, was, 0, 0, 0))
    except (ImportError, OSError, AttributeError):
        pass

    def restore() -> None:
        for f in reversed(undo):
            try:
                f()
            except (OSError, ValueError):
                pass

    return restore


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

    old = termios.tcgetattr(r)
    new = old[:]
    new[3] &= ~termios.ECHO
    termios.tcsetattr(r, termios.TCSAFLUSH, new)  # echo off before the prompt: fast input is neither shown nor flushed
    try:
        os.write(w, prompt.encode("utf-8", "replace"))
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


_PLAIN_REVIEW = {"shown": False, "hint": True}


def note_plain_review(hint: bool = True) -> None:
    """The plain review of what is about to be signed was shown on the person's terminal: the passphrase prompt that
    follows asks for the technical block with ``d`` instead of printing it (everything stays reachable)."""
    _PLAIN_REVIEW["shown"] = True
    _PLAIN_REVIEW["hint"] = hint  # N13: False when the person already read the full review (no point in offering it)


def tty_passphrase_provider(request: PassphraseRequest) -> str:
    """Ask on the person's own terminal (see the module docstring). Refuses with :class:`NoPrompt` without one."""
    r, w, close = _open_tty()
    compact = _PLAIN_REVIEW["shown"]
    hint = _PLAIN_REVIEW["hint"]
    _PLAIN_REVIEW["shown"] = False  # one-shot: a later signature in this process never inherits it
    _PLAIN_REVIEW["hint"] = True
    try:
        if request.kind in ("unlock", "batch") and compact:
            # A plain review was just shown on this terminal (B11-P6, finding 24): one line instead of the technical
            # block. `d` is never a valid passphrase (too short), so it is safe to take it as the question.
            # N13: the hint is shown only where `d` works here (not after the details were already read); the prompt
            # keeps ending in "Passphrase: " (the dev-pair and e2e terminal drivers answer that text)
            first = _read_secret(r, w, "\nPress d for technical details\nPassphrase: " if hint else "\nPassphrase: ")
            while first == "d":
                first = _read_secret(r, w, render_prompt(request) + "Passphrase: ")
        else:
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


def _check_purposes(value: object, *, creating: bool = False) -> list[str]:
    """A kx key's purposes: a non-empty list of distinct :data:`KX_PURPOSES` members, sorted (on disk exactly so)."""
    err: Exception = (
        CustodyError(f"a kx key holds a non-empty set of these purposes only: {', '.join(KX_PURPOSES)}")
        if creating
        else ValueError("purposes")
    )
    if type(value) not in ((list, tuple) if creating else (list,)) or not value:
        raise err
    if not all(type(v) is str for v in value):  # type: ignore[union-attr]
        raise err
    out = sorted(value) if creating else list(value)  # type: ignore[arg-type]
    if out != sorted(set(out)) or not set(out) <= set(KX_PURPOSES):
        raise err
    return out


def _check_recipient(value: object) -> bytes:
    if type(value) is not str or len(value) != 32 or value != value.lower():
        raise ValueError("recipient")
    return bytes.fromhex(value)


def _kx_rule(doc: dict, purpose: object, object_id: object, epoch: object, extra: object, sealed: object) -> bytes:
    """The checks before a ``kx`` key opens anything; returns the recipient id fixed in the key file."""
    if doc["role"] != "kx":
        raise CustodyError("only a kx key opens a seal; a signing key never does ECDH")
    if type(purpose) is not str or purpose not in doc["purposes"]:
        raise CustodyError(f"this kx key does not open purpose {purpose!r}")
    want_epoch, want_extra = KX_PURPOSE_RULES[purpose]
    if type(epoch) is not int or epoch != want_epoch or type(extra) is not bytes or extra != want_extra:
        raise CustodyError(f"a {purpose} seal is opened at epoch {want_epoch} with no other AAD")
    if type(object_id) is not bytes or len(object_id) != 16 or type(sealed) is not bytes:
        raise CustodyError("the object id is 16 bytes and the sealed item is bytes")
    return bytes.fromhex(doc["recipient"])


@dataclass(frozen=True)
class PrimarySession:
    """What :meth:`PassphraseBackend.primary_unlocked` yields: the pair's public identity and two capabilities,
    ``sign(payload)`` (relay logins only) and ``unseal(purpose, object_id, epoch, extra, sealed)``."""

    device_id: bytes
    dk_sig_pub: bytes
    dk_kx_pub: bytes
    sign: Callable[[bytes], bytes] = field(repr=False)
    unseal: Callable[[str, bytes, int, bytes, bytes], bytes] = field(repr=False)


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
        code calls ``PassphraseBackend(directory)`` and gets the terminal prompt and the 2^15 floor, or
        :meth:`answering` for the dashboard's custody switch."""
        if not _params_ok(_kdf, _min_n):
            raise CustodyError("unacceptable scrypt parameters")
        self._dir = files.private_dir(directory)
        self._provider = _passphrase_provider or tty_passphrase_provider
        self._kdf = _kdf
        self._min_n = _min_n

    @classmethod
    def answering(cls, directory: str | Path, passphrase: str) -> CustodyChangeBackend:
        """A backend whose one prompt is answered with ``passphrase`` instead of the terminal, for the dashboard's
        click-to-sign switch (orch-v2-host §0.8.6 item 3a): the person types the passphrase into the dashboard and the
        click signer passes it on. It is not a :class:`PassphraseBackend`: it only reads the public key, exports the
        scalar for the one custody change and deletes the old file (:class:`CustodyChangeBackend`), so text that never
        came from ``/dev/tty`` cannot sign, unlock or unseal (Opus M2 on #539). Only
        :func:`orch.custody.select.stage_convenience` uses it (an AST test pins the callers); every other setting
        (the scrypt floor included) is production's."""
        return CustodyChangeBackend(cls(directory, _passphrase_provider=lambda _request: passphrase))

    @classmethod
    def generated(cls, directory: str | Path, passphrase: str) -> PassphraseBackend:
        """A full backend whose prompt is answered with ``passphrase``: the one ``orch setup``'s own process generated
        in memory for this run (B9). It can sign and unlock one batch. Only :func:`orch.custody.select.setup_backend`
        calls it (an AST test pins that); a passphrase from a browser, an argument or a file never reaches it."""
        return cls(directory, _passphrase_provider=lambda _request: passphrase)

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
                type(doc) is not dict
                or doc.get("role") not in ROLES
                or set(doc) != (_KX_FIELDS if doc["role"] == "kx" else _FIELDS)
                or doc["v"] != 1
                or doc["key_id"] != key_id
            ):
                raise ValueError("shape")
            if doc["role"] == "kx":
                _check_purposes(doc["purposes"])
                _check_recipient(doc["recipient"])
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
        if doc["role"] == "kx":  # authenticated with the rest of the header: a changed purpose or recipient never opens
            header["purposes"], header["recipient"] = doc["purposes"], doc["recipient"]
        return doc, header, params, salt, nonce, ct

    # -- interface ----------------------------------------------------------------------------------------------------

    def create(
        self,
        key_id: str,
        *,
        secret: bytes | None = None,
        role: str = "device",
        passphrase: str | None = None,
        purposes: tuple[str, ...] | None = None,
        recipient: bytes | None = None,
    ) -> bytes:
        """See :meth:`_create_key`. ``primary`` and ``kx`` keys are refused here: only :meth:`create_primary` makes
        them, with the backend's own prompt and the recipient fixed to the pair's device id (Opus Low on #512)."""
        if role in ("primary", "kx"):
            raise CustodyError(f"a {role} key is made only with the primary pair (create_primary)")
        return self._create_key(
            key_id, secret=secret, role=role, passphrase=passphrase, purposes=purposes, recipient=recipient
        )

    def _create_key(
        self,
        key_id: str,
        *,
        secret: bytes | None,
        role: str,
        passphrase: str | None,
        purposes: tuple[str, ...] | None,
        recipient: bytes | None,
    ) -> bytes:
        """Create the key. ``passphrase`` is for a caller that has already asked the person itself on the terminal
        (``orch setup`` offers a generated one); without it the backend's own prompt asks (twice). Either way the text
        must pass :func:`orch.custody.strength.check_strength`.

        ``role="kx"`` makes a key-exchange key: ``purposes`` (a subset of :data:`KX_PURPOSES`) and ``recipient`` (the
        16-byte recipient id the seals name) are required and written into the authenticated header. ``kx`` and
        ``primary`` keys are always generated here, never imported (``secret`` is refused)."""
        check_key_id(key_id)
        if role not in ROLES:
            raise CustodyError(f"the passphrase backend holds person, device, primary and kx keys, not {role!r}")
        extra_header: dict[str, object] = {}
        if role == "kx":
            if purposes is None or type(recipient) is not bytes or len(recipient) != 16:
                raise CustodyError("a kx key is created with its purposes and its 16-byte recipient id")
            extra_header = {"purposes": _check_purposes(purposes, creating=True), "recipient": recipient.hex()}
        elif purposes is not None or recipient is not None:
            raise CustodyError("purposes and a recipient belong to kx keys only")
        if role in ("kx", "primary") and secret is not None:
            raise CustodyError(f"a {role} key is generated in custody, never imported")
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
        header = {"v": 1, "key_id": key_id, "role": role, "pub": crypto.b64u(pub), "kdf": kdf, **extra_header}
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

    @contextlib.contextmanager
    def unlocked(
        self,
        key_id: str,
        *,
        action: str,
        digest: str,
        labels: tuple[bytes, ...],
        fields: tuple[tuple[str, str], ...] = (),
    ) -> Iterator[Callable[[bytes], bytes]]:
        """One passphrase for a batch of signatures (``orch import v1``, ticket-format 14): ask once, with ``action``,
        the ``digest`` of what the batch will sign and ``fields`` describing it; hold the unlocked key in this process
        for the ``with`` block only; yield ``sign(payload)``, which refuses every payload ``sign`` would refuse. The
        scalar is zeroised when the block ends, however it ends. ``labels`` limits what the key signs to those domain
        labels (``orch import v1`` passes the ticket-event label only), on top of the role's own. From before the
        passphrase prompt to the end of the block, core dumps are off and a debugger cannot attach (:func:`_harden`).
        Per-signature prompts (D65) stay the rule everywhere else: only a caller that shows the person the whole
        batch first may use this (a test checks that only ``ops/import_run.py`` calls it)."""
        if not labels or not all(label_allowed(lb, ROLE_LABELS["device"]) for lb in labels):
            raise CustodyError("an unlocked batch names the device labels it may sign")
        check_key_id(key_id)
        doc, header, params, salt, nonce, ct = self._read(key_id)
        if doc["role"] != "device":
            raise CustodyError("only a device key signs a batch")
        request = PassphraseRequest("batch", key_id, action if isinstance(action, str) else "", digest, fields)
        restore = _harden()  # before the prompt: the passphrase, the derived key and the scalar are all covered
        try:
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
                allowed = labels
                live = [key]

                def sign(payload: bytes) -> bytes:
                    if not live:
                        raise CustodyError("the key is locked again")
                    if not label_allowed(payload, allowed):
                        raise CustodyError(
                            f"a {doc['role']} key does not sign this payload (unknown label or another role's label)"
                        )
                    return crypto.sign(live[0], payload)

                try:
                    yield sign
                finally:
                    live.clear()
            finally:
                _zeroise(derived)
                _zeroise(scalar)
                key = None  # noqa: F841
        finally:
            restore()

    def export_for_custody_change(self, key_id: str, *, fields: tuple[tuple[str, str], ...]) -> bytearray:
        """The device key's 32-byte scalar, for one custody change (``orch keys custody convenience``,
        orch-v2-host §0.8.6): one passphrase prompt (kind ``convert``, ``fields`` say where the key goes), with core
        dumps off and no debugger attach until the process ends. The caller zeroises the returned buffer. Only
        :mod:`orch.custody.select` calls it (a test checks). Device keys only: a person key is never exported."""
        check_key_id(key_id)
        doc, header, params, salt, nonce, ct = self._read(key_id)
        if doc["role"] != "device":
            raise CustodyError("only a device key changes custody")
        _harden()  # stays on for the rest of this short-lived command
        pw = _passphrase_bytes(self._provider(PassphraseRequest("convert", key_id, "", "", fields)))
        if not pw:
            raise WrongPassphrase("empty passphrase")
        derived = bytearray(_derive(pw, salt, params))
        try:
            try:
                scalar = bytearray(crypto.aes_gcm_open(bytes(derived), nonce, ct, self._aad(header)))
            except InvalidTag:
                raise WrongPassphrase("wrong passphrase") from None
        finally:
            _zeroise(derived)
        key = crypto.private_key_from_scalar(int.from_bytes(scalar, "big"))
        if crypto.public_bytes(key) != crypto.unb64u(doc["pub"], crypto.PUB_LEN):
            _zeroise(scalar)
            raise CustodyError("key file does not match its public key")
        return scalar

    # -- key exchange (orch-v2-host 11A.15.10 item 9) ------------------------------------------------------------------

    def _decrypt(self, loaded: tuple, pw: bytes) -> bytearray:
        """The scalar of a loaded key file (the caller zeroises it); :class:`WrongPassphrase` when it does not open."""
        doc, header, params, salt, nonce, ct = loaded
        derived = bytearray(_derive(pw, salt, params))
        try:
            try:
                return bytearray(crypto.aes_gcm_open(bytes(derived), nonce, ct, self._aad(header)))
            except InvalidTag:
                raise WrongPassphrase("wrong passphrase") from None
        finally:
            _zeroise(derived)

    @staticmethod
    def _key_of(doc: dict, scalar: bytearray):
        key = crypto.private_key_from_scalar(int.from_bytes(scalar, "big"))
        if crypto.public_bytes(key) != crypto.unb64u(doc["pub"], crypto.PUB_LEN):
            raise CustodyError("key file does not match its public key")
        return key

    def unseal(self, key_id: str, purpose: str, object_id: bytes, epoch: int, extra: bytes, sealed: bytes) -> bytes:
        """See :meth:`orch.custody.base.Backend.unseal`. One passphrase prompt per call (kind ``unseal``); the rules
        are checked before the prompt. A command that opens and signs several times uses
        :meth:`primary_unlocked` instead."""
        check_key_id(key_id)
        loaded = self._read(key_id)
        doc = loaded[0]
        rcpt = _kx_rule(doc, purpose, object_id, epoch, extra, sealed)
        fields = (("purpose", purpose), ("object", object_id.hex()), ("epoch", str(epoch)))
        request = PassphraseRequest("unseal", key_id, "", crypto.sha256(sealed).hex()[:32], fields)
        pw = _passphrase_bytes(self._provider(request))
        if not pw:
            raise WrongPassphrase("empty passphrase")
        scalar = self._decrypt(loaded, pw)
        key = None
        try:
            key = self._key_of(doc, scalar)
            return crypto.open_sealed(key, rcpt, purpose, object_id, epoch, sealed, extra)
        finally:
            _zeroise(scalar)
            key = None  # noqa: F841

    def create_primary(self, sig_key_id: str, kx_key_id: str, *, purposes: tuple[str, ...]) -> tuple[bytes, bytes]:
        """The primary key pair under one passphrase (the backend's own prompt, once, asked twice): ``sig_key_id``
        (role ``primary``) and ``kx_key_id`` (role ``kx``, ``purposes``, recipient = the protocol device id of the
        signing key). If the second key cannot be made, the first is removed. Returns ``(dk_sig_pub, dk_kx_pub)``.
        No caller supplies the passphrase (only ``orch setup`` does that, for the workspace key)."""
        check_key_id(sig_key_id)
        check_key_id(kx_key_id)
        if sig_key_id == kx_key_id:
            raise CustodyError("the primary's two keys need two key ids")
        passphrase = self._provider(PassphraseRequest("create", sig_key_id, "", ""))
        sig_pub = self._create_key(
            sig_key_id, secret=None, role="primary", passphrase=passphrase, purposes=None, recipient=None
        )
        try:
            kx_pub = self._create_key(
                kx_key_id,
                secret=None,
                role="kx",
                passphrase=passphrase,
                purposes=purposes,
                recipient=crypto.device_id(sig_pub),
            )
        except BaseException:
            with contextlib.suppress(KeyNotFound):
                self.delete(sig_key_id)
            raise
        return sig_pub, kx_pub

    @contextlib.contextmanager
    def primary_unlocked(
        self, sig_key_id: str, kx_key_id: str, *, action: str, fields: tuple[tuple[str, str], ...] = ()
    ) -> Iterator[PrimarySession]:
        """The one-command unlock of the primary role (11A.15.10 item 9): one passphrase prompt (kind ``primary``)
        opens both keys of the pair for the ``with`` block; the yielded :class:`PrimarySession` signs only
        :data:`PRIMARY_SIG_LABELS` (the relay login) and opens only the ``kx`` key's purposes, under the same rules as
        :meth:`unseal`. Both keys are zeroised when the block ends, however it ends; core dumps and debugger attach
        are off from before the prompt (:func:`_harden`). The two files must be a pair (the ``kx`` recipient is the
        signing key's device id) under one passphrase. Only :mod:`orch.custody.primary` calls it (a test checks)."""
        check_key_id(sig_key_id)
        check_key_id(kx_key_id)
        sig_loaded, kx_loaded = self._read(sig_key_id), self._read(kx_key_id)
        sig_doc, kx_doc = sig_loaded[0], kx_loaded[0]
        if sig_doc["role"] != "primary" or kx_doc["role"] != "kx":
            raise CustodyError("the primary unlock opens a primary signing key and a kx key")
        sig_pub = crypto.unb64u(sig_doc["pub"], crypto.PUB_LEN)
        kx_pub = crypto.unb64u(kx_doc["pub"], crypto.PUB_LEN)
        if kx_doc["recipient"] != crypto.device_id(sig_pub).hex():
            raise CustodyError("the kx key does not belong to this primary signing key")
        request = PassphraseRequest("primary", sig_key_id, action if isinstance(action, str) else "", "", fields)
        restore = _harden()
        sig_scalar, kx_scalar = bytearray(), bytearray()
        live: list = []
        try:
            pw = _passphrase_bytes(self._provider(request))
            if not pw:
                raise WrongPassphrase("empty passphrase")
            sig_scalar = self._decrypt(sig_loaded, pw)
            try:
                kx_scalar = self._decrypt(kx_loaded, pw)
            except WrongPassphrase:  # the same passphrase opened the signing key: the pair's files are damaged
                raise CustodyError(
                    "the primary key files are damaged: the passphrase opens the signing key but not the kx key"
                ) from None
            live += [self._key_of(sig_doc, sig_scalar), self._key_of(kx_doc, kx_scalar)]

            def sign(payload: bytes) -> bytes:
                if not live:
                    raise CustodyError("the primary key is locked again")
                if not label_allowed(payload, PRIMARY_SIG_LABELS):
                    raise CustodyError("the primary signs relay logins only")
                return crypto.sign(live[0], payload)

            def unseal(purpose: str, object_id: bytes, epoch: int, extra: bytes, sealed: bytes) -> bytes:
                if not live:
                    raise CustodyError("the primary key is locked again")
                rcpt = _kx_rule(kx_doc, purpose, object_id, epoch, extra, sealed)
                return crypto.open_sealed(live[1], rcpt, purpose, object_id, epoch, sealed, extra)

            yield PrimarySession(crypto.device_id(sig_pub), sig_pub, kx_pub, sign, unseal)
        finally:
            live.clear()
            _zeroise(sig_scalar)
            _zeroise(kx_scalar)
            restore()

    def key_info(self, key_id: str) -> dict:
        """The public part of a key file (role, public key, and for ``kx`` its purposes and recipient). Never
        prompts, never decrypts."""
        check_key_id(key_id)
        doc, *_ = self._read(key_id)
        out = {"key_id": key_id, "role": doc["role"], "pub": doc["pub"]}
        if doc["role"] == "kx":
            out["purposes"], out["recipient"] = list(doc["purposes"]), doc["recipient"]
        return out

    def presence(self) -> str:
        return self.auth or "none"

    def exists(self, key_id: str) -> bool:
        return self._path(key_id).exists()

    def delete(self, key_id: str) -> None:
        files.delete_file(self._path(key_id))


class CustodyChangeBackend:
    """What :meth:`PassphraseBackend.answering` returns: the three calls a conversion to convenience needs and nothing
    else. ``sign``, ``unlocked``, ``unseal``, ``primary_unlocked`` and ``create`` do not exist here."""

    __slots__ = ("_backend",)

    def __init__(self, backend: PassphraseBackend) -> None:
        self._backend = backend

    def public_key(self, key_id: str) -> bytes:
        return self._backend.public_key(key_id)

    def export_for_custody_change(self, key_id: str, *, fields: tuple[tuple[str, str], ...]) -> bytearray:
        return self._backend.export_for_custody_change(key_id, fields=fields)

    def delete(self, key_id: str) -> None:
        self._backend.delete(key_id)
