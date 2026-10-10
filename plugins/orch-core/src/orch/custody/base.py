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
* a backend with ``person_capable = False`` (the ``file`` tier) signs without a factor and can never sign a person
  event, a certificate, a revocation or a delegation.
"""

from __future__ import annotations

import re
from typing import Protocol, runtime_checkable

from orch import canon
from orch.crypto import labels

__all__ = [
    "AUTH_VALUES",
    "HOST_LABELS",
    "PERSON_LABELS",
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


# What a person-tier key (a person key PK or a device key dk_sig behind a human factor) signs. Host-only labels are
# excluded: a key is either a person's or the workspace's, never both.
PERSON_LABELS: tuple[bytes, ...] = (
    _e("sig_ticket_event"),
    _e("sig_ws_event"),
    _p("sig_device_cert"),
    _p("sig_revocation"),
    _p("sig_ws_delegation"),
    _p("sig_cert_challenge"),
    _p("sig_decision"),
    _p("sig_bridge"),
    _p("sig_enroll_request"),
    _p("sig_ws_cosign"),
    _p("sig_webauthn_bind"),
    _p("sig_relay_auth"),
)

# What the workspace key (file tier on a VPS, keychain on a laptop) signs. No label here is a person's.
HOST_LABELS: tuple[bytes, ...] = (
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
)


def label_allowed(payload: bytes, allowed: tuple[bytes, ...]) -> bool:
    """True iff ``payload`` starts with one of ``allowed``. The label sets are prefix-free (tested)."""
    return type(payload) is bytes and any(payload.startswith(a) for a in allowed)


@runtime_checkable
class Backend(Protocol):
    """What ``orch`` needs from a key store. All methods are synchronous and raise :class:`CustodyError`."""

    name: str
    auth: str | None  # the ``auth`` value a person event signed by this backend carries; None for the file tier
    person_capable: bool

    def create(self, key_id: str, *, secret: bytes | None = None) -> bytes:
        """Make a P-256 signing key (or import ``secret``, the 32-byte scalar, for a key derived from the recovery
        code) and return its 65-byte public key. Refuses an existing ``key_id``."""

    def public_key(self, key_id: str) -> bytes:
        """The 65-byte public key. Never prompts."""

    def sign(self, key_id: str, payload: bytes, *, action: str) -> bytes:
        """The 64-byte signature of ``payload``. ``action`` is the human words shown at a prompt."""

    def presence(self) -> str:
        """The factor a signature needs: ``passphrase`` (or another :data:`AUTH_VALUES` name), or ``none``."""

    def exists(self, key_id: str) -> bool: ...

    def delete(self, key_id: str) -> None: ...
