"""The custody interface (D64, D66; ticket-format §5.3, §12 O2): one small surface every key backend implements.

``sign(key_id, payload, action=...)`` returns the 64-byte raw P-256 signature of ``payload`` and nothing else leaves
a backend: no method returns a private key. ``payload`` is the *signed bytes* (a domain label followed by canonical
JSON, built by :mod:`orch.canon` or :mod:`orch.identity`); a backend refuses a payload that does not start with a
signature label it is allowed to sign (see :func:`label_allowed`), so a key cannot be talked into signing arbitrary
bytes or into signing under the wrong role.

Tiers (ticket-format §5.3: "a device key that can sign without the factor its ``auth`` names is a ``file``-tier key
and never signs person events"):

* a backend with ``person_capable = True`` signs only behind its human factor and records ``auth`` (one of
  :data:`AUTH_VALUES`) in every person event;
* a backend with ``person_capable = False`` (the ``file`` tier) signs without a factor, holds only the ``workspace``
  role and can never sign a person event, a certificate, a revocation or a delegation.

Errors: a flat :class:`CustodyError` hierarchy with stable ``code`` tokens (``custody.*``). ``orch.identity.Refused``
carries ``code`` tokens too; C6 maps both through one table to ``OrchError`` codes, with ``custody.no_prompt`` and
``custody.wrong_passphrase`` kept distinct (agents branch on them, D65).
"""

from __future__ import annotations

import re
from typing import Protocol, runtime_checkable

from orch import canon
from orch.crypto import labels

__all__ = [
    "AUTH_VALUES",
    "HOST_LABELS",
    "PERSON_KEY_LABELS",
    "PERSON_LABELS",
    "DEVICE_KEY_LABELS",
    "ROLE_LABELS",
    "Backend",
    "BackendUnavailable",
    "CustodyError",
    "KeyExists",
    "KeyNotFound",
    "NoPrompt",
    "WrongPassphrase",
    "check_key_id",
    "label_allowed",
]

AUTH_VALUES = ("passphrase", "secure-enclave", "secure-enclave-unlocked", "webauthn", "tpm", "windows-hello")
"""Ticket-format §11.4 ``auth`` values (custody metadata, signed, not a proof)."""


class CustodyError(Exception):
    """Base class; ``code`` is a short stable token."""

    code = "custody"

    def __init__(self, message: str = "") -> None:
        super().__init__(message or self.code)


class BackendUnavailable(CustodyError):
    code = "custody.unavailable"


class NoPrompt(CustodyError):
    """No terminal to ask on and no injected passphrase callback: a human-only signature is refused (D65)."""

    code = "custody.no_prompt"


class WrongPassphrase(CustodyError):
    code = "custody.wrong_passphrase"


class KeyNotFound(CustodyError):
    code = "custody.key_not_found"


class KeyExists(CustodyError):
    code = "custody.key_exists"


_KEY_ID = re.compile(r"[a-z0-9][a-z0-9._-]{0,63}")


def check_key_id(key_id: object) -> str:
    """Key ids become file names: lower-case, digits, ``.``, ``_``, ``-``; no separators, no leading dot."""
    if type(key_id) is not str or not _KEY_ID.fullmatch(key_id) or ".." in key_id:
        raise CustodyError(f"bad key id {key_id!r}")
    return key_id


def _p(key: str) -> bytes:
    return labels.L[key]


def _e(key: str) -> bytes:
    return canon.LABELS[key].encode("ascii")


# Per key role (protocol §3 table, "IKM / signer"). A key has one role, fixed at creation, and signs only that role's
# labels. Decisions: ``sig_bridge`` and ``sig_relay_auth`` are signed by ``dk_sig`` (requests) and ``WSK`` (responses,
# logins), so they are on both lists, but the file tier can only ever hold the ``workspace`` role;
# ``sig_enroll_request``
# is signed by the new agent device key (``device``); ``sig_drop_object`` by an author ``dk_sig`` or ``WSK``;
# ``sig_sk_grant`` (the Drop space owner's key, undefined before P2) is on no list until Drop exists.
PERSON_KEY_LABELS: tuple[bytes, ...] = (  # PK: certificates, revocations, delegations, cert challenges
    _p("sig_device_cert"),
    _p("sig_revocation"),
    _p("sig_ws_delegation"),
    _p("sig_cert_challenge"),
)
DEVICE_KEY_LABELS: tuple[bytes, ...] = (  # dk_sig: person events and device requests
    _e("sig_ticket_event"),
    _e("sig_ws_event"),
    _p("sig_decision"),
    _p("sig_bridge"),
    _p("sig_enroll_request"),
    _p("sig_ws_cosign"),
    _p("sig_webauthn_bind"),
    _p("sig_relay_auth"),
    _p("sig_drop_object"),
)
HOST_LABELS: tuple[bytes, ...] = (  # WSK: host events and workspace-signed objects
    _e("sig_host_event"),
    _e("sig_checkpoint"),
    _p("sig_card_wsk"),
    _p("sig_push"),
    _p("sig_member_list"),
    _p("sig_wk_grant"),
    _p("sig_bridge"),
    _p("sig_cert_request"),
    _p("sig_drop_claim"),
    _p("sig_ws_envelope"),
    _p("sig_publish"),
    _p("sig_relay_auth"),
    _p("sig_drop_object"),
)
ROLE_LABELS: dict[str, tuple[bytes, ...]] = {
    "person": PERSON_KEY_LABELS,
    "device": DEVICE_KEY_LABELS,
    "workspace": HOST_LABELS,
}
PERSON_LABELS = PERSON_KEY_LABELS + DEVICE_KEY_LABELS  # every label a human-factor key may sign (either role)


def label_allowed(payload: bytes, allowed: tuple[bytes, ...]) -> bool:
    """True iff ``payload`` starts with one of ``allowed``. The label sets are prefix-free (tested)."""
    return type(payload) is bytes and any(payload.startswith(a) for a in allowed)


@runtime_checkable
class Backend(Protocol):
    """What ``orch`` needs from a key store. All methods are synchronous and raise :class:`CustodyError`."""

    name: str
    auth: str | None  # the ``auth`` value a person event signed by this backend carries; None for the file tier
    person_capable: bool

    def create(self, key_id: str, *, secret: bytes | None = None, role: str = "device") -> bytes:
        """Make a P-256 signing key (or import ``secret``, the 32-byte scalar, for a key derived from the recovery
        code) and return its 65-byte public key. ``role`` is ``person`` (PK), ``device`` (dk_sig) or ``workspace``
        (WSK) and fixes which labels the key signs. Refuses an existing ``key_id``, atomically."""

    def public_key(self, key_id: str) -> bytes:
        """The 65-byte public key. Never prompts."""

    def sign(self, key_id: str, payload: bytes, *, action: str) -> bytes:
        """The 64-byte signature of ``payload``. ``action`` is the human words shown at a prompt."""

    def presence(self) -> str:
        """The factor a signature needs: always ``auth`` if it is set, else ``"none"`` (the file tier)."""

    def exists(self, key_id: str) -> bool: ...

    def delete(self, key_id: str) -> None: ...
