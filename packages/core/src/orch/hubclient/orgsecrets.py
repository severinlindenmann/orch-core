"""Company secrets sealed to a workspace's exchange key (orch-hub ``contracts/hub/protocol.md`` 7.5, M3 #1431-#1433).

An admin's browser seals a value to each workspace's WXK (seal purpose ``org-secret``); the hub only stores the sealed
bytes. The host opens a wrap here. One value rule is enforced in the browser, in the hub API and, after unsealing, on
the host: visible ASCII 0x20-0x7E, no leading or trailing space, 8..4096 characters (the value becomes a Bearer header).

AAD: the standard seal AAD (purpose ``org-secret``, object = secret id, epoch = secret version) plus
``extra = hub_id(16) || workspace_id(16) || u32(wxk_version) || u64(expires_ms) || u8(len(name)) || name``. The
recipient id in the KDF info is the workspace id. ``contracts/hub/vectors_org_secrets.json`` is replayed whole by this
module, the hub's mirror and the browser module."""

from __future__ import annotations

import re
import struct

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.asymmetric.ec import EllipticCurvePrivateKey

from orch.crypto import sealing, suite

__all__ = [
    "EXPIRY_MAX_MS",
    "PURPOSE",
    "VALUE_MAX",
    "VALUE_MIN",
    "OrgSecretRefused",
    "open_org_secret",
    "seal_org_secret",
    "value_problem",
    "wrap_aad_extra",
    "wrap_inputs",
]

PURPOSE = "org-secret"
VALUE_MIN = 8
VALUE_MAX = 4096
EXPIRY_MAX_MS = 366 * 24 * 3600 * 1000
NAME = re.compile(r"[a-z][a-z0-9-]{0,39}")
_VALUE = re.compile(r"[\x20-\x7e]+")
_HEX16 = re.compile(r"[0-9a-f]{32}")


class OrgSecretRefused(ValueError):
    """A wrap or value is refused; ``code`` is one of ``aead``, ``value``, ``expired``, ``horizon``, ``malformed``."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def value_problem(value: str) -> str | None:
    """``None`` if ``value`` is acceptable, else ``too_short``, ``too_long``, ``bad_character`` or ``edge_space``."""
    if type(value) is not str:
        return "bad_character"
    if len(value) < VALUE_MIN:
        return "too_short"
    if len(value) > VALUE_MAX:
        return "too_long"
    if _VALUE.fullmatch(value) is None:
        return "bad_character"
    if value[0] == " " or value[-1] == " ":
        return "edge_space"
    return None


def _id16(x: object) -> bytes:
    if type(x) is not str or _HEX16.fullmatch(x) is None:
        raise OrgSecretRefused("malformed")
    return bytes.fromhex(x)


def _uint(x: object, bits: int) -> int:
    if type(x) is not int or not 0 <= x < 2**bits:
        raise OrgSecretRefused("malformed")
    return x


def wrap_aad_extra(hub_id: str, workspace_id: str, wxk_version: int, expires_ms: int, name: str) -> bytes:
    if type(name) is not str or NAME.fullmatch(name) is None:
        raise OrgSecretRefused("malformed")
    n = name.encode("ascii")
    return (
        _id16(hub_id)
        + _id16(workspace_id)
        + struct.pack(">I", _uint(wxk_version, 32))
        + struct.pack(">Q", _uint(expires_ms, 64))
        + bytes([len(n)])
        + n
    )


def wrap_inputs(
    hub_id: str,
    workspace_id: str,
    secret_id: str,
    version: int,
    wxk_version: int,
    expires_ms: int,
    name: str,
    value: str,
    check_value: bool = True,
) -> tuple[bytes, str, bytes, int, bytes, bytes]:
    """The seal inputs ``(rcpt, purpose, obj, version, pt, extra)`` for one wrap (no randomness).

    ``check_value=False`` lets tests build a sealed bad value."""
    if check_value and value_problem(value) is not None:
        raise OrgSecretRefused("value")
    extra = wrap_aad_extra(hub_id, workspace_id, wxk_version, expires_ms, name)
    rcpt, obj = _id16(workspace_id), _id16(secret_id)
    _uint(version, 32)
    return rcpt, PURPOSE, obj, version, value.encode("utf-8"), extra


def seal_org_secret(
    wxk_pub: bytes,
    hub_id: str,
    workspace_id: str,
    secret_id: str,
    version: int,
    wxk_version: int,
    expires_ms: int,
    name: str,
    value: str,
) -> bytes:
    """Seal ``value`` to ``wxk_pub`` (fresh ephemeral key). Refuses a value that fails :func:`value_problem`."""
    inputs = wrap_inputs(hub_id, workspace_id, secret_id, version, wxk_version, expires_ms, name, value)
    try:
        return sealing.seal(wxk_pub, *inputs)
    except suite.CryptoError:
        raise OrgSecretRefused("malformed") from None


def open_org_secret(
    wxk_priv: EllipticCurvePrivateKey,
    hub_id: str,
    workspace_id: str,
    secret_id: str,
    version: int,
    wxk_version: int,
    expires_ms: int,
    name: str,
    sealed: bytes,
    now_ms: int,
    issued_ms: int,
) -> str:
    """Open a wrap. Raises :class:`OrgSecretRefused` on any AEAD failure, a value that fails the rule after unsealing,
    ``expires_ms <= now_ms`` (``expired``) or ``expires_ms > issued_ms + EXPIRY_MAX_MS`` (``horizon``)."""
    extra = wrap_aad_extra(hub_id, workspace_id, wxk_version, expires_ms, name)
    rcpt, obj = _id16(workspace_id), _id16(secret_id)
    _uint(version, 32)
    _uint(now_ms, 64)
    _uint(issued_ms, 64)
    if expires_ms <= now_ms:
        raise OrgSecretRefused("expired")
    if expires_ms > issued_ms + EXPIRY_MAX_MS:
        raise OrgSecretRefused("horizon")
    try:
        pt = sealing.open_sealed(wxk_priv, rcpt, PURPOSE, obj, version, sealed, extra)
    except (InvalidTag, suite.CryptoError):
        raise OrgSecretRefused("aead") from None
    try:
        value = pt.decode("ascii")
    except UnicodeDecodeError:
        raise OrgSecretRefused("value") from None
    if value_problem(value) is not None:
        raise OrgSecretRefused("value")
    return value
