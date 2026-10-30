"""The person key ``PK`` as a pluggable source (orch-v2-host 11A.15.10 item 10; owner decision Q1 = B, 11A.15.16).

``PK`` is the person's **permanent** identity key: ``person_id = id(PK)``, there is no rotation, and a stolen copy
stays valid. ``orch setup`` derives it from the 24-word recovery code and drops it (core rule C8). Adding a device
(the certificate, the cert challenge or the rejection of §8.4) and revoking one need it again. A
:class:`PersonKeySource` is what those commands get instead of a key:

* ``person_id`` / ``pk_pub``: public;
* ``sign(label, payload)``: only for :data:`SIGN_LABELS` (a subset of
  :data:`orch.custody.base.PERSON_KEY_LABELS`), and only a payload that starts with that label's bytes;
  ``signer(label)`` is the same, bound to one label, as the ``sign`` callable the ``orch.identity`` builders take;
* ``derive_label_key()``: the one derived key a command needs, ``HKDF(pk_scalar, "", "orch/v2/pvk|p1")``, exactly
  ``orch.ops.workspace_init._label_key`` (it seals and opens device labels). Nothing else of the scalar leaves;
* a context manager: the key exists only inside the ``with`` block and is zeroised (best effort) when the block
  ends, however it ends; ``sign`` after the block is :class:`PersonKeyClosed`.

**``sig_ws_delegation`` is not signed here (JUDGEMENT CALL, H12c-d).** The delegation of a workspace key is signed
once, by ``orch setup``, from the code it has just shown. Adding and revoking devices (R6, revocation) never need it,
and a source that cannot sign one cannot be talked into delegating a workspace to an attacker's ``WSK``. This is the
same set 11A.15.16 fixes for the A' option. A future caller that needs it adds it with its own review.

:class:`RecoveryCodeSource` is the production source under Q1 = B: the code is typed on ``/dev/tty`` (never stdin;
refused without a terminal, :class:`~orch.custody.base.NoPrompt`, and under ``ORCH_GRANT``, :class:`HumanOnly`)
**once** for the command, checked by the existing parser (:func:`orch.identity.recovery.check_recovery_code`, the
BIP-39 checksum), derived exactly as ``orch setup`` derives it (:func:`orch.identity.recovery.derive_person_scalar`),
and matched against the expected person (the workspace owner's pinned ``pk_pub`` or ``person_id``) **before** any
signature (:class:`PersonMismatch`). Nothing is written to disk; the code and the key are never logged, never in an
exception text, an exception chain or its frames, or a ``repr``. The terminal is opened first; from then, before the
prompt, to the end of the block core dumps are off and a debugger cannot attach (the passphrase backend's
``_harden``), so a refusal without a terminal sets nothing irreversible. The ``action`` is shown printable characters
only. The source signs only the labels its command names (``labels=``, required). **Best effort, stated honestly
(Opus F6):** the module zeroises the buffers it owns (the typed bytes, the 32-byte scalar) and drops every reference,
but these immutable copies stay in memory until it is reused: the decoded code text (a ``str``) and the bytes the
parser and scrypt make from it; the scalar as a Python ``int`` and the temporary ``bytes`` of its ``to_bytes``; the
``bytes`` copy of the scalar that :meth:`RecoveryCodeSource.derive_label_key` passes to HKDF, and the label key it
returns; and OpenSSL's copy inside the key object. A ``SIGKILL`` while the prompt waits leaves the terminal without
echo (``stty echo`` restores it), as with ``getpass``. A program running as the user at that moment can capture the
code (11A.15.16, "what every same-Mac option shares").

The only environment variable read is ``ORCH_GRANT``, and it only narrows (refuses), never selects (11A.15.11 N8a).
The sandbox's source of the dev path lives in the test tree (``tests/relay/personkey_sandbox.py``), never here.
"""

from __future__ import annotations

import contextlib
import hmac
import os
import signal
import threading
import traceback
from collections.abc import Callable, Iterable, Iterator
from typing import Any, Protocol, runtime_checkable

from orch import crypto
from orch.crypto.labels import L
from orch.identity import recovery

from .base import PERSON_KEY_LABELS, CustodyError, label_allowed
from .passphrase import _harden, _open_tty, _zeroise

__all__ = [
    "LABEL_KEY_INFO",
    "MAX_ACTION_CHARS",
    "MAX_CODE_BYTES",
    "SIGN_LABELS",
    "BadRecoveryCode",
    "HumanOnly",
    "LabelRefused",
    "PersonKeyClosed",
    "PersonKeyError",
    "PersonKeySource",
    "PersonMismatch",
    "PublicKeyUnknown",
    "RecoveryCodeSource",
    "TypedCodeSource",
    "check_granted",
    "check_labels",
    "check_payload",
    "printable_action",
]

SIGN_LABELS: tuple[str, ...] = (
    "sig_device_cert", "sig_cert_challenge", "sig_cert_rejection", "sig_revocation", "sig_device_rotate",
    "sig_hub_owner_proof", "sig_remote_owner_proof",
)  # fmt: skip
"""What a person-key source signs: certificates, cert challenges and rejections (R6) and revocations. Never
``sig_ws_delegation`` (see the module docstring)."""
assert all(L[n] in PERSON_KEY_LABELS for n in SIGN_LABELS)

LABEL_KEY_INFO = b"orch/v2/pvk|p1"
"""The HKDF ``info`` of the device-label key (``workspace_init._label_key``): the only derivation offered."""

MAX_CODE_BYTES = 512
"""The longest line accepted at the prompt (24 words of at most 8 letters need under 220 bytes)."""

MAX_ACTION_CHARS = 120
"""The longest ``action`` shown at the prompt (after removing every non-printable character)."""

_REASONS = ("recovery.words", "recovery.unknown_word", "recovery.checksum")


class HumanOnly(CustodyError):
    """Under ``ORCH_GRANT`` (an agent's environment): the person key is never asked for. The ops code ``human_only``."""

    code = "human_only"


class BadRecoveryCode(CustodyError):
    """The typed text is not a valid recovery code; ``reason`` is the parser's ``recovery.words``,
    ``recovery.unknown_word`` or ``recovery.checksum``. Nothing of the text is in the message."""

    code = "custody.recovery_code"

    def __init__(self, reason: str) -> None:
        super().__init__(f"that is not a valid recovery code ({reason}); nothing was signed")
        self.reason = reason


class PersonMismatch(CustodyError):
    """A valid code of **another** person: refused before any signature."""

    code = "custody.person_mismatch"


class PersonKeyClosed(CustodyError):
    """``sign`` or ``derive_label_key`` outside the ``with`` block, or a source entered a second time."""

    code = "custody.person_key_closed"


class PersonKeyError(CustodyError):
    """Deriving the person key from a valid code failed (for example out of memory); nothing of the code or the
    failure's frames is kept."""

    code = "custody.person_key"


class PublicKeyUnknown(PersonKeyClosed, AttributeError):
    """``pk_pub`` of a source named by ``person_id`` before its code is typed (also an ``AttributeError``)."""


class LabelRefused(CustodyError):
    """A label outside :data:`SIGN_LABELS`, or a payload that does not start with the label it names."""

    code = "custody.label_refused"


@runtime_checkable
class PersonKeySource(Protocol):
    """What a command that needs ``PK`` gets (11A.15.10 item 10). Implementations: :class:`RecoveryCodeSource` (Q1 =
    B); the dev sandbox's in the test tree; ``SecureEnclaveKey`` in phase 3."""

    @property
    def person_id(self) -> bytes:
        """The 16-byte protocol person id."""
        ...

    @property
    def pk_pub(self) -> bytes:
        """The 65-byte public person key (known once the block is entered)."""
        ...

    def sign(self, label: str, payload: bytes) -> bytes:
        """The 64-byte signature of ``payload``, which starts with ``L[label]``; ``label`` in :data:`SIGN_LABELS`."""
        ...

    def signer(self, label: str) -> Callable[[bytes], bytes]:
        """``sign`` bound to ``label``: the ``sign`` callable of the ``orch.identity`` builders."""
        ...

    def derive_label_key(self) -> bytes:
        """``HKDF(pk_scalar, "", "orch/v2/pvk|p1")``, the device-label key."""
        ...

    def __enter__(self) -> PersonKeySource: ...

    def __exit__(self, *exc: object) -> bool | None: ...


def check_payload(label: object, payload: object) -> bytes:
    """The label policy every source applies: ``label`` is one of :data:`SIGN_LABELS` and ``payload`` (bytes) starts
    with its domain bytes. Returns the label bytes or raises :class:`LabelRefused`."""
    if type(label) is not str or label not in SIGN_LABELS:
        raise LabelRefused("the person key signs device certificates, cert challenges, rejections and revocations only")
    want = L[label]
    if type(payload) is not bytes or not payload.startswith(want) or not label_allowed(payload, PERSON_KEY_LABELS):
        raise LabelRefused(f"the payload is not a {label} payload")
    return want


def check_labels(labels: object) -> frozenset[str]:
    """The labels one command may sign (least privilege, Opus F4): a non-empty collection of names from
    :data:`SIGN_LABELS` (adding a device: challenge, rejection, certificate; revoking: revocation)."""
    if isinstance(labels, (str, bytes)) or not isinstance(labels, Iterable):
        raise LabelRefused("name the labels this command signs, as a collection of label names")
    names = list(labels)
    if not names or not all(type(n) is str and n in SIGN_LABELS for n in names):
        raise LabelRefused("the labels must be a non-empty subset of " + ", ".join(SIGN_LABELS))
    return frozenset(names)


def check_granted(granted: frozenset[str], label: object, payload: object) -> None:
    """:func:`check_payload`, and ``label`` is one this source was given."""
    check_payload(label, payload)
    if label not in granted:
        raise LabelRefused(f"this command did not ask to sign {label}")


def printable_action(action: object) -> str:
    """What the prompt shows of ``action`` (Opus F2): whitespace becomes a space, every other non-printable character
    (C0 and C1 controls such as ESC and CSI, bidi and other format characters, unassigned code points) is removed,
    runs of spaces collapse, at most :data:`MAX_ACTION_CHARS` characters."""
    if not isinstance(action, str):
        return ""
    kept = "".join(" " if c.isspace() else c for c in action if c.isspace() or c.isprintable())
    return " ".join(kept.split())[:MAX_ACTION_CHARS]


def _expected(person_id: object, pk_pub: object) -> tuple[bytes, bytes | None]:
    if (person_id is None) == (pk_pub is None):
        raise CustodyError("name the expected person: exactly one of person_id or pk_pub")
    if pk_pub is not None:
        if type(pk_pub) is not bytes:
            raise CustodyError("pk_pub must be the 65-byte public key")
        try:
            return crypto.person_id(pk_pub), pk_pub
        except Exception:  # noqa: BLE001 - any invalid point
            raise CustodyError("pk_pub is not a valid public key") from None
    if type(person_id) is not bytes or len(person_id) != 16:
        raise CustodyError("person_id must be the 16-byte protocol person id")
    return person_id, None


def _derive_scalar(text: str) -> int:
    """The person scalar of a checked code: ``orch setup``'s derivation (production scrypt parameters). Tests replace
    it with the low-N seam; nothing in production does."""
    return recovery.derive_person_scalar(text)


@contextlib.contextmanager
def _echo_back_on_signal(fd: int, old: list[Any]) -> Iterator[None]:
    """While the prompt waits (Opus F5): a ``SIGTERM`` or ``SIGHUP`` whose handler is the default (which would end the
    process without running ``finally`` blocks, leaving the terminal without echo) first puts the terminal's settings
    back and the previous handlers, then re-raises the signal with its default action. A signal with a Python handler
    or ignored is left alone (a handler's exception unwinds through the ``finally`` that restores echo). Only the main
    thread can install handlers; elsewhere this is a no-op, as ``getpass`` is."""
    import termios

    if threading.current_thread() is not threading.main_thread():
        yield
        return
    saved = {s: signal.SIG_DFL for s in (signal.SIGTERM, signal.SIGHUP) if signal.getsignal(s) is signal.SIG_DFL}

    def handler(signum: int, frame: object) -> None:
        with contextlib.suppress(OSError, termios.error):
            termios.tcsetattr(fd, termios.TCSANOW, old)
        for s, h in saved.items():
            signal.signal(s, h)
        signal.raise_signal(signum)

    for s in saved:
        signal.signal(s, handler)
    try:
        yield
    finally:
        for s, h in saved.items():
            signal.signal(s, h)


def _read_code(r: int, w: int, prompt: str, buf: bytearray) -> bool:
    """Write ``prompt`` and read one line, echo off, into the caller's ``buf`` (so the caller zeroises it on every
    path, a ``KeyboardInterrupt`` included). Returns False when the line was longer than :data:`MAX_CODE_BYTES` (the
    rest of the line is read and dropped). The terminal's settings are restored however it ends, a default-action
    ``SIGTERM``/``SIGHUP`` included (:func:`_echo_back_on_signal`)."""
    import termios

    old = termios.tcgetattr(r)
    new = old[:]
    new[3] &= ~termios.ECHO
    fits = True
    c = b""
    with _echo_back_on_signal(r, old):
        termios.tcsetattr(r, termios.TCSAFLUSH, new)
        try:
            os.write(w, prompt.encode("utf-8", "replace"))
            while True:
                c = os.read(r, 1)
                if not c or c == b"\n":
                    break
                if len(buf) < MAX_CODE_BYTES:
                    buf += c
                else:
                    fits = False
        finally:
            c = b""  # noqa: F841 - the last byte read is not left in a frame an exception carries
            termios.tcsetattr(r, termios.TCSAFLUSH, old)
            os.write(w, b"\n")
    if buf.endswith(b"\r"):
        del buf[-1]
    return fits


def _unlock(typed: bytearray, scalar: bytearray, derive: Callable[[str], int] | None = None) -> Any:
    """Check ``typed`` with the existing parser, derive the person key as ``orch setup`` does, write its 32-byte scalar
    into the caller's ``scalar`` and return the key object. Shared by every source that starts from a code.

    **No typed text survives in an exception (Opus F1).** A parser refusal records only its reason inside the
    ``except`` and :class:`BadRecoveryCode` is raised after it, so it has no ``__context__``; the parser's frames
    (which hold the text) are cleared first. A failed derivation of a valid code clears the failure's frames and
    raises a fresh :class:`PersonKeyError` (or a fresh exception of the same type for a ``BaseException`` such as
    ``KeyboardInterrupt``; a ``SystemExit`` keeps an ``int`` exit code) without cause or context; so does any other
    exception or interrupt inside the parser itself (Opus N1). The caller zeroises ``typed`` and, on failure,
    ``scalar``."""
    reason: str | None = None
    failed: BaseException | None = None
    text: str | None = None
    try:
        text = recovery.check_recovery_code(bytes(typed).decode("utf-8"))
    except UnicodeDecodeError as e:
        traceback.clear_frames(e.__traceback__)
        reason = "recovery.words"
    except recovery.RecoveryCodeError as e:
        traceback.clear_frames(e.__traceback__)
        reason = e.code if e.code in _REASONS else "recovery.words"
    except BaseException as e:  # noqa: BLE001 - Opus N1: an interrupt inside the parser; replaced below
        traceback.clear_frames(e.__traceback__)
        failed = _fresh(e)
    if failed is not None:
        text = None
        raise failed
    if reason is not None:
        raise BadRecoveryCode(reason)
    key: Any = None
    d: int | None = None
    try:
        d = (derive or _derive_scalar)(text)  # type: ignore[arg-type]
        text = None
        scalar += d.to_bytes(32, "big")
        key = crypto.private_key_from_scalar(d)
    except BaseException as e:  # noqa: BLE001 - re-raised below without the frames that hold the code
        traceback.clear_frames(e.__traceback__)
        failed = _fresh(e)
    text = d = None
    if failed is None:
        return key
    key = None
    raise failed


def _fresh(e: BaseException) -> BaseException:
    """The replacement for ``e`` (raised by the caller outside its ``except``, so without context): an ``Exception``
    becomes :class:`PersonKeyError`; any other ``BaseException`` a new one of the same type with no arguments, except
    that a ``SystemExit`` keeps an ``int`` or ``None`` exit code (Opus N2; a ``str`` code is dropped, it could be
    text)."""
    if isinstance(e, Exception):
        return PersonKeyError("the person key could not be derived from the recovery code; nothing was signed")
    if isinstance(e, SystemExit):
        code = e.code if e.code is None or type(e.code) is int else None
        return SystemExit(code)
    try:
        return type(e)()
    except Exception:  # noqa: BLE001 - a BaseException subclass that needs arguments
        return KeyboardInterrupt()


def _prompt(action: str, person_id: bytes) -> str:
    return (
        "\n=== orch: your recovery code is needed ===\n"
        f"For: {action}\n"
        f"Person: p_{person_id.hex()}\n"
        "Type your 24-word recovery code (it is not shown). It is your permanent person key: orch keeps it in memory\n"
        "for this command only, then wipes it; nothing is stored. Only type it for an action you started yourself.\n"
        "Recovery code: "
    )


class RecoveryCodeSource:
    """The person key from the recovery code typed on ``/dev/tty``, for one command (Q1 = B). ``person_id`` (16
    bytes) or ``pk_pub`` (65 bytes) names the person the code must belong to (the workspace owner's pinned key);
    ``action`` is the human words shown at the prompt. Use once::

        with RecoveryCodeSource(pk_pub=owner_pk_pub, action="add the device phone") as pk:
            challenge = make_cert_challenge(pk.pk_pub, pk.signer("sig_cert_challenge"), request, inner, nonce)
    """

    __slots__ = ("_action", "_labels", "_live", "_person_id", "_pk_pub", "_restore", "_state")

    def __init__(
        self,
        *,
        labels: Iterable[str],
        person_id: bytes | None = None,
        pk_pub: bytes | None = None,
        action: str,
    ) -> None:
        self._labels = check_labels(labels)
        self._person_id, self._pk_pub = _expected(person_id, pk_pub)
        self._action = printable_action(action)
        self._live: list[Any] = []  # [private key, 32-byte scalar bytearray] inside the block only
        self._restore: Callable[[], None] | None = None
        self._state = "new"

    # -- public facts -------------------------------------------------------------------------------------------------

    @property
    def person_id(self) -> bytes:
        return self._person_id

    @property
    def pk_pub(self) -> bytes:
        """The expected key when it was given, else known once the code is typed. Before that an
        ``AttributeError``-compatible :class:`PublicKeyUnknown`, so a runtime protocol check (Python 3.11 ``hasattr``)
        never fails with another exception."""
        if self._pk_pub is None:
            raise PublicKeyUnknown("the public key is known once the recovery code has been typed")
        return self._pk_pub

    def __repr__(self) -> str:
        return f"RecoveryCodeSource(person=p_{self._person_id.hex()}, state={self._state})"

    __str__ = __repr__

    def __reduce_ex__(self, protocol: object) -> Any:
        raise TypeError("a person-key source cannot be copied or pickled")

    def __copy__(self) -> Any:
        raise TypeError("a person-key source cannot be copied or pickled")

    def __deepcopy__(self, memo: object) -> Any:
        raise TypeError("a person-key source cannot be copied or pickled")

    # -- the lifecycle ------------------------------------------------------------------------------------------------

    def __enter__(self) -> RecoveryCodeSource:
        if self._state != "new":
            raise PersonKeyClosed("a recovery code source serves one command; make a new one")
        self._state = "closed"  # one prompt per source, whatever happens next
        if os.environ.get("ORCH_GRANT") is not None:
            raise HumanOnly("the recovery code is never asked for under ORCH_GRANT: an agent never holds PK")
        typed, scalar = bytearray(), bytearray()
        key: Any = None
        restore: Callable[[], None] | None = None
        opened = False
        # The terminal first (Opus F3): without one the refusal comes before the irreversible PT_DENY_ATTACH.
        r, w, close = _open_tty()
        try:
            try:
                restore = _harden()  # before the prompt: the typed code, the scalar and the key are all covered
                fits = _read_code(r, w, _prompt(self._action, self._person_id), typed)
            finally:
                close()
            if not fits:
                raise BadRecoveryCode("recovery.words")
            key = _unlock(typed, scalar)
            pub = crypto.public_bytes(key)
            if not hmac.compare_digest(crypto.person_id(pub), self._person_id) or (
                self._pk_pub is not None and not hmac.compare_digest(pub, self._pk_pub)
            ):
                key = None
                raise PersonMismatch(
                    f"that recovery code is not the one of p_{self._person_id.hex()}; nothing was signed"
                )
            self._pk_pub = pub
            self._live[:] = [key, scalar]
            self._restore = restore
            self._state = "open"
            opened = True
            return self
        finally:
            key = None
            _zeroise(typed)
            if not opened:
                self._live.clear()
                _zeroise(scalar)
                if restore is not None:
                    restore()

    def __exit__(self, *exc: object) -> bool:
        self._close()
        return False

    def __del__(self) -> None:
        """A source entered by hand and never closed is closed when it is collected (best effort)."""
        try:
            if self._live:
                self._close()
        except Exception:  # noqa: BLE001, S110 - never raise from a finaliser
            pass

    def _close(self) -> None:
        live, self._live = self._live, []
        self._state = "closed"
        try:
            for item in live:
                if isinstance(item, bytearray):
                    _zeroise(item)
            live.clear()
        finally:
            restore, self._restore = self._restore, None
            if restore is not None:
                restore()

    def _key(self) -> Any:
        if self._state != "open" or not self._live:
            raise PersonKeyClosed("the person key is not unlocked: sign inside the `with` block of one command")
        return self._live[0]

    # -- what the key does --------------------------------------------------------------------------------------------

    def sign(self, label: str, payload: bytes) -> bytes:
        check_granted(self._labels, label, payload)
        return crypto.sign(self._key(), payload)

    def signer(self, label: str) -> Callable[[bytes], bytes]:
        check_granted(self._labels, label, L[label] if label in SIGN_LABELS else None)

        def sign(payload: bytes) -> bytes:
            return self.sign(label, payload)

        return sign

    def derive_label_key(self) -> bytes:
        self._key()
        return crypto.hkdf(bytes(self._live[1]), b"", LABEL_KEY_INFO)


class TypedCodeSource(RecoveryCodeSource):
    """The person key from a recovery code the click signer opened from the sealed field (#1198 C10, purpose
    ``hub.backup_key``), for one proof. The same lifecycle and checks as :class:`RecoveryCodeSource` (the code is
    parsed and the key derived by the one existing path; the derived person must be ``person_id``/``pk_pub`` before
    anything is signed; only the granted labels sign; no raw-sign call; one use), but the code arrives as bytes, not as
    a prompt on ``/dev/tty``. **It takes ownership of ``code`` (a ``bytearray``) and wipes it on entering**, whatever
    happens; the scalar is wiped on exit. Under ``ORCH_GRANT`` it refuses, like its sibling: an agent never holds PK."""

    __slots__ = ("_code",)

    def __init__(
        self,
        code: bytearray,
        *,
        labels: Iterable[str],
        person_id: bytes | None = None,
        pk_pub: bytes | None = None,
        action: str = "",
    ) -> None:
        try:
            super().__init__(labels=labels, person_id=person_id, pk_pub=pk_pub, action=action)
        except BaseException:
            _zeroise(code)
            raise
        self._code: bytearray | None = code

    def __enter__(self) -> TypedCodeSource:
        typed, self._code = self._code, None
        scalar = bytearray()
        opened = False
        try:
            if self._state != "new" or typed is None:
                raise PersonKeyClosed("a recovery code source serves one command; make a new one")
            self._state = "closed"
            if os.environ.get("ORCH_GRANT") is not None:
                raise HumanOnly("the recovery code is never used under ORCH_GRANT: an agent never holds PK")
            key = _unlock(typed, scalar)
            pub = crypto.public_bytes(key)
            if not hmac.compare_digest(crypto.person_id(pub), self._person_id) or (
                self._pk_pub is not None and not hmac.compare_digest(pub, self._pk_pub)
            ):
                key = None
                raise PersonMismatch(
                    f"that recovery code is not the one of p_{self._person_id.hex()}; nothing was signed"
                )
            self._pk_pub = pub
            self._live[:] = [key, scalar]
            self._state = "open"
            opened = True
            return self
        finally:
            if typed is not None:
                _zeroise(typed)
            if not opened:
                self._live.clear()
                _zeroise(scalar)

    def __del__(self) -> None:
        code, self._code = getattr(self, "_code", None), None
        if code is not None:
            _zeroise(code)
        super().__del__()
