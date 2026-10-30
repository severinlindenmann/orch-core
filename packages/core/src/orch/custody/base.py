"""The custody interface (D64, D66; ticket-format §5.3, §12 O2): one small surface every key backend implements.

``sign(key_id, payload, action=...)`` returns the 64-byte raw P-256 signature of ``payload`` and nothing else leaves
a backend: no method returns a private key. ``payload`` is the *signed bytes* (a domain label followed by canonical
JSON, built by :mod:`orch.canon` or :mod:`orch.identity`); a backend refuses a payload that does not start with a
signature label it is allowed to sign (see :func:`label_allowed`), so a key cannot be talked into signing arbitrary
bytes or into signing under the wrong role.

Tiers (ticket-format §5.3: "a device key that can sign without the factor its ``auth`` names is a ``file``-tier key
and never signs person events"):

* a backend with ``person_capable = True`` signs person events and records ``auth`` (one of :data:`AUTH_VALUES`) in
  every one: behind its human factor, except ``convenience`` (orch-v2-host §0.8.6, D-P2-23), which asks for none and
  declares that in ``auth``;
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
    "KX_PURPOSES",
    "KX_PURPOSE_RULES",
    "PERSON_KEY_LABELS",
    "PRIMARY_SIG_LABELS",
    "PERSON_LABELS",
    "DEVICE_KEY_LABELS",
    "REMOTE_PRESENCE_LABELS",
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

AUTH_VALUES = (
    "passphrase",
    "secure-enclave",
    "secure-enclave-unlocked",
    "convenience",
    "webauthn",
    "tpm",
    "windows-hello",
)
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
PERSON_KEY_LABELS: tuple[bytes, ...] = (  # PK: certificates, revocations, delegations, cert challenges, rejections
    _p("sig_device_cert"),
    _p("sig_revocation"),
    _p("sig_ws_delegation"),
    _p("sig_cert_challenge"),
    _p("sig_cert_rejection"),
    _p("sig_device_rotate"),  # #1097: a device rotation, bound to one workspace, old and new device and roster_v
    _p("sig_hub_owner_proof"),  # #1198 C9: the owner proof of a hub join, mode switch or leave (hub contract 9.6)
    _p("sig_remote_owner_proof"),  # R2: the owner proof of a remote policy change or a device confirmation (10.2c)
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
    _e("sig_reader_decl"),  # H12d: the device's own reader declaration (orch-v2-host §0.8.6 item 9.3)
    _p("sig_genesis_pin"),  # B1: the genesis pin of a first link (relay.genesis_pin, orch-v2-host 11A.2)
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
    _p("sig_hub_binding"),  # V3 C2a: the workspace's statement that a hub serves it, and its leave (hub contract 9)
    _p("sig_hub_unbound"),
    _p("sig_hub_link"),  # V3 C2a: link requests and the link login (hub contract 10)
    _p("sig_hub_link_auth"),
    _p("sig_publish_hub"),  # V3 C7: publish through the hub (hub contract 11.1)
    _p("sig_space_item"),  # V3 C13: an item the workspace puts into a shared space (hub contract 10.4)
    _p("sig_space_accept"),  # V3 C13: the owner's consent to join a space (hub contract 10.4)
    _p("sig_share_item"),  # V3 C6: an item a selective workspace shares with its hub (hub contract 10.2b)
    _p("sig_remote_devices"),  # R2 #1590: the remote device list (hub contract 10.2c)
)
PRIMARY_SIG_LABELS: tuple[bytes, ...] = (_p("sig_relay_auth"),)
"""The primary's ``dk_sig`` (orch-v2-host 11A.15.1): it only logs in to the relay as the account's device (protocol
§12.2, ``POST /v2/accounts``, ``PUT /v2/devices``). The primary is never an F1 member, so it signs no person event,
decision or bridge request; certificates, challenges and rejections are ``PK``'s (``PERSON_KEY_LABELS``)."""
REMOTE_PRESENCE_LABELS: tuple[bytes, ...] = (_p("sig_remote_open"), _p("sig_remote_presence"))
"""R5 (#1593, D64 kind ``remote-presence``): the presence key of a computer used for remote access signs exactly
these two messages of hub contract 10.2c (the stream open and an action presence). It is not ``person_capable``, never
offered by :mod:`orch.custody.select` and has no ``file`` tier: it lives in a Secure Enclave / security key
(``orch.remoteclient``)."""
ROLE_LABELS: dict[str, tuple[bytes, ...]] = {
    "person": PERSON_KEY_LABELS,
    "device": DEVICE_KEY_LABELS,
    "workspace": HOST_LABELS,
    "primary": PRIMARY_SIG_LABELS,
    "remote-presence": REMOTE_PRESENCE_LABELS,  # R5: see REMOTE_PRESENCE_LABELS; no custody backend creates this role
    "kx": (),  # a key-exchange key signs nothing; a signing key never does ECDH (11A.15.1)
}

KX_PURPOSES: tuple[str, ...] = ("cert-request",)
"""The seal purposes (protocol §5.3) a custody ``kx`` key may hold, fixed per key at creation in its authenticated
header. v1.7 has one ``kx`` key, the primary's, and it opens cert requests only (11A.15.1, C2): never ``wk``,
``card`` or ``ws-envelope`` (the primary is never a member). A new purpose is added here with its own rule below."""
KX_PURPOSE_RULES: dict[str, tuple[int, bytes]] = {"cert-request": (0, b"")}
"""Per purpose, the only ``(epoch, extra_aad)`` an unseal accepts: a cert request is sealed at epoch 0 with no extra
AAD (protocol §8.4 step 1)."""
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

    def unseal(self, key_id: str, purpose: str, object_id: bytes, epoch: int, extra: bytes, sealed: bytes) -> bytes:
        """Open ``sealed`` (protocol §5.3) with the ``kx`` key ``key_id``, to the recipient fixed in its file. Refuses
        (:class:`CustodyError`, before any prompt) a key that is not ``kx``, a ``purpose`` the key does not hold, and
        an ``(epoch, extra)`` the purpose does not allow; a blob that does not open raises
        :class:`cryptography.exceptions.InvalidTag`. Backends without ``kx`` keys refuse every call."""

    def exists(self, key_id: str) -> bool: ...

    def delete(self, key_id: str) -> None: ...
