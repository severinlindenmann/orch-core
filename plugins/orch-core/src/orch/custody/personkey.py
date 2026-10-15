"""The person key ``PK`` as a pluggable source (orch-v2-host 11A.15.10 item 10; owner decision Q1 = B, 11A.15.16).

``PK`` is the person's **permanent** identity key: ``person_id = id(PK)``, there is no rotation, and a stolen copy
stays valid. ``orch init`` derives it from the 24-word recovery code and drops it (core rule C8). Adding a device
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
once, by ``orch init``, from the code it has just shown. Adding and revoking devices (R6, revocation) never need it,
and a source that cannot sign one cannot be talked into delegating a workspace to an attacker's ``WSK``. This is the
same set 11A.15.16 fixes for the A' option. A future caller that needs it adds it with its own review.

:class:`RecoveryCodeSource` is the production source under Q1 = B: the code is typed on ``/dev/tty`` (never stdin;
refused without a terminal, :class:`~orch.custody.base.NoPrompt`, and under ``ORCH_GRANT``, :class:`HumanOnly`)
**once** for the command, checked by the existing parser (:func:`orch.identity.recovery.check_recovery_code`, the
BIP-39 checksum), derived exactly as ``orch init`` derives it (:func:`orch.identity.recovery.derive_person_scalar`),
and matched against the expected person (the workspace owner's pinned ``pk_pub`` or ``person_id``) **before** any
signature (:class:`PersonMismatch`). Nothing is written to disk; the code and the key are never logged, never in an
exception text or a ``repr``. From before the prompt to the end of the block core dumps are off and a debugger cannot
attach (the passphrase backend's ``_harden``). **Best effort, stated honestly:** Python keeps an immutable copy of the
decoded code text and of the scalar as an ``int`` until the garbage collector reuses the memory, and OpenSSL holds
the key object's copy; the module zeroises the buffers it owns (the typed bytes, the 32-byte scalar) and drops every
reference. A program running as the user at that moment can capture the code (11A.15.16, "what every same-Mac option
shares").

The only environment variable read is ``ORCH_GRANT``, and it only narrows (refuses), never selects (11A.15.11 N8a).
The sandbox's source of the dev path lives in the test tree (``tests/relay/personkey_sandbox.py``), never here.
"""

from __future__ import annotations

import hmac
import os
from collections.abc import Callable
from typing import Any, Protocol, runtime_checkable

from orch import crypto
from orch.crypto.labels import L
from orch.identity import recovery

from .base import PERSON_KEY_LABELS, CustodyError, label_allowed
from .passphrase import _harden, _open_tty, _zeroise

__all__ = [
    "LABEL_KEY_INFO",
    "MAX_CODE_BYTES",
    "SIGN_LABELS",
    "BadRecoveryCode",
    "HumanOnly",
    "LabelRefused",
    "PersonKeyClosed",
    "PersonKeySource",
    "PersonMismatch",
    "RecoveryCodeSource",
    "check_payload",
]

SIGN_LABELS: tuple[str, ...] = ("sig_device_cert", "sig_cert_challenge", "sig_cert_rejection", "sig_revocation")
"""What a person-key source signs: certificates, cert challenges and rejections (R6) and revocations. Never
``sig_ws_delegation`` (see the module docstring)."""
assert all(L[n] in PERSON_KEY_LABELS for n in SIGN_LABELS)

LABEL_KEY_INFO = b"orch/v2/pvk|p1"
"""The HKDF ``info`` of the device-label key (``workspace_init._label_key``): the only derivation offered."""

MAX_CODE_BYTES = 512
"""The longest line accepted at the prompt (24 words of at most 8 letters need under 220 bytes)."""


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
    """The person scalar of a checked code: ``orch init``'s derivation (production scrypt parameters). Tests replace
    it with the low-N seam; nothing in production does."""
    return recovery.derive_person_scalar(text)


def _read_code(r: int, w: int, prompt: str, buf: bytearray) -> bool:
    """Write ``prompt`` and read one line, echo off, into the caller's ``buf`` (so the caller zeroises it on every
    path, a ``KeyboardInterrupt`` included). Returns False when the line was longer than :data:`MAX_CODE_BYTES` (the
    rest of the line is read and dropped). The terminal's settings are restored however it ends."""
    import termios

    old = termios.tcgetattr(r)
    new = old[:]
    new[3] &= ~termios.ECHO
    termios.tcsetattr(r, termios.TCSAFLUSH, new)
    fits = True
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
        termios.tcsetattr(r, termios.TCSAFLUSH, old)
        os.write(w, b"\n")
    if buf.endswith(b"\r"):
        del buf[-1]
    return fits


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

    __slots__ = ("_action", "_live", "_person_id", "_pk_pub", "_restore", "_state")

    def __init__(self, *, person_id: bytes | None = None, pk_pub: bytes | None = None, action: str) -> None:
        self._person_id, self._pk_pub = _expected(person_id, pk_pub)
        self._action = " ".join(action.split())[:200] if isinstance(action, str) else ""
        self._live: list[Any] = []  # [private key, 32-byte scalar bytearray] inside the block only
        self._restore: Callable[[], None] | None = None
        self._state = "new"

    # -- public facts -------------------------------------------------------------------------------------------------

    @property
    def person_id(self) -> bytes:
        return self._person_id

    @property
    def pk_pub(self) -> bytes:
        if self._pk_pub is None:
            raise PersonKeyClosed("the public key is known once the recovery code has been typed")
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
        restore = _harden()  # before the prompt: the typed code, the scalar and the key are all covered
        typed, scalar = bytearray(), bytearray()
        text: str | None = None
        d: int | None = None
        key: Any = None
        opened = False
        try:
            r, w, close = _open_tty()
            try:
                fits = _read_code(r, w, _prompt(self._action, self._person_id), typed)
            finally:
                close()
            if not fits:
                raise BadRecoveryCode("recovery.words")
            try:
                text = recovery.check_recovery_code(bytes(typed).decode("utf-8"))
            except UnicodeDecodeError:
                raise BadRecoveryCode("recovery.words") from None
            except recovery.RecoveryCodeError as e:
                raise BadRecoveryCode(e.code) from None
            d = _derive_scalar(text)
            text = None
            scalar += d.to_bytes(32, "big")
            key = crypto.private_key_from_scalar(d)
            d = None
            pub = crypto.public_bytes(key)
            if not hmac.compare_digest(crypto.person_id(pub), self._person_id) or (
                self._pk_pub is not None and not hmac.compare_digest(pub, self._pk_pub)
            ):
                key = None
                raise PersonMismatch(
                    f"that recovery code is not the one of p_{self._person_id.hex()}; nothing was signed"
                ) from None
            self._pk_pub = pub
            self._live[:] = [key, scalar]
            self._restore = restore
            self._state = "open"
            opened = True
            return self
        finally:
            text = d = key = None  # noqa: F841 - drop the frame's references before an exception carries the frame
            _zeroise(typed)
            if not opened:
                self._live.clear()
                _zeroise(scalar)
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
        check_payload(label, payload)
        return crypto.sign(self._key(), payload)

    def signer(self, label: str) -> Callable[[bytes], bytes]:
        check_payload(label, L[label] if label in SIGN_LABELS else None)

        def sign(payload: bytes) -> bytes:
            return self.sign(label, payload)

        return sign

    def derive_label_key(self) -> bytes:
        self._key()
        return crypto.hkdf(bytes(self._live[1]), b"", LABEL_KEY_INFO)
