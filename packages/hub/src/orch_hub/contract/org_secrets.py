"""Company secrets sealed to a workspace's exchange key (protocol.md 7.5, M3 #1431-#1433): the hub's copy of the rule.

The hub never holds a value or a key that opens one. It checks the shape of a value and of a wrap, and the browser
module ``web/seal.js`` and the host (``orch.hubclient.orgsecrets``) run the same construction. It stands alone (D79: no
import from orch-core); ``contracts/hub/vectors_org_secrets.json`` is replayed whole by all three.

Seal: purpose ``org-secret``, recipient id = workspace id, object = secret id, epoch = secret version, AAD extra =
``hub_id || workspace_id || u32(wxk_version) || u64(expires_ms) || u8(len(name)) || name``, value rule: visible ASCII
0x20-0x7E, no leading or trailing space, 8..4096 characters."""

from __future__ import annotations

import re
import struct

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from . import suite as S
from .labels import L

PURPOSE = "org-secret"
VALUE_MIN = 8
VALUE_MAX = 4096
EXPIRY_MAX_MS = 366 * 24 * 3600 * 1000
NAME = re.compile(r"[a-z][a-z0-9-]{0,39}")
_VALUE = re.compile(r"[\x20-\x7e]+")
_HEX16 = re.compile(r"[0-9a-f]{32}")
SECRETS_MAX = 50
#: a sealed wrap: eph_pub(65) + GCM(value 8..4096 bytes) + tag(16)
SEALED_MIN = S.PUB_LEN + VALUE_MIN + 16
SEALED_MAX = S.PUB_LEN + VALUE_MAX + 16


class OrgSecretRefused(ValueError):
    """``code`` is one of ``aead``, ``value``, ``expired``, ``horizon``, ``malformed``."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def value_problem(value: str) -> str | None:
    """``None`` if acceptable, else ``too_short``, ``too_long``, ``bad_character`` or ``edge_space``."""
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


def _aad(object_id: bytes, version: int, extra: bytes) -> bytes:
    p = PURPOSE.encode("ascii")
    return L["aad_seal"] + bytes([S.SUITE_ID, len(p)]) + p + object_id + struct.pack(">I", _uint(version, 32)) + extra


def _key(ss: bytes, eph_pub: bytes, rcpt_pub: bytes, workspace_id: str) -> bytes:
    info = b"orch/v2/seal|" + PURPOSE.encode() + b"|" + workspace_id.encode("ascii")
    return HKDF(algorithm=hashes.SHA256(), length=32, salt=eph_pub + rcpt_pub, info=info).derive(ss)


def _seal_with_ephemeral(
    eph: ec.EllipticCurvePrivateKey,
    wxk_pub: bytes,
    hub_id: str,
    workspace_id: str,
    secret_id: str,
    version: int,
    wxk_version: int,
    expires_ms: int,
    name: str,
    value: str,
    check_value: bool = True,
) -> bytes:
    """Test seam for the vectors (fixed ephemeral; ``check_value=False`` builds a sealed bad value). Tests only."""
    if check_value and value_problem(value) is not None:
        raise OrgSecretRefused("value")
    extra = wrap_aad_extra(hub_id, workspace_id, wxk_version, expires_ms, name)
    obj = _id16(secret_id)
    _id16(workspace_id)
    if not S.valid_public_key(wxk_pub):
        raise OrgSecretRefused("malformed")
    eph_pub = S.public_bytes(eph)
    ss = eph.exchange(ec.ECDH(), ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), wxk_pub))
    key = _key(ss, eph_pub, wxk_pub, workspace_id)
    return eph_pub + AESGCM(key).encrypt(bytes(12), value.encode("utf-8"), _aad(obj, version, extra))


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
    """Seal with a fresh ephemeral key (the real sealing is the browser's; this is for tests and tools)."""
    return _seal_with_ephemeral(
        ec.generate_private_key(ec.SECP256R1()),
        wxk_pub,
        hub_id,
        workspace_id,
        secret_id,
        version,
        wxk_version,
        expires_ms,
        name,
        value,
    )


def open_org_secret(
    wxk_priv: ec.EllipticCurvePrivateKey,
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
    extra = wrap_aad_extra(hub_id, workspace_id, wxk_version, expires_ms, name)
    _id16(workspace_id)
    obj = _id16(secret_id)
    aad = _aad(obj, version, extra)
    _uint(now_ms, 64)
    _uint(issued_ms, 64)
    if expires_ms <= now_ms:
        raise OrgSecretRefused("expired")
    if expires_ms > issued_ms + EXPIRY_MAX_MS:
        raise OrgSecretRefused("horizon")
    if type(sealed) is not bytes or len(sealed) < S.PUB_LEN + 16:
        raise OrgSecretRefused("aead")
    eph_pub, ct = sealed[: S.PUB_LEN], sealed[S.PUB_LEN :]
    try:
        if not S.valid_public_key(eph_pub):
            raise InvalidTag
        ss = wxk_priv.exchange(ec.ECDH(), ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), eph_pub))
        if not any(ss):
            raise InvalidTag
        pt = AESGCM(_key(ss, eph_pub, S.public_bytes(wxk_priv), workspace_id)).decrypt(bytes(12), ct, aad)
    except InvalidTag, ValueError:
        raise OrgSecretRefused("aead") from None
    try:
        value = pt.decode("ascii")
    except UnicodeDecodeError:
        raise OrgSecretRefused("value") from None
    if value_problem(value) is not None:
        raise OrgSecretRefused("value")
    return value


def sealed_shape_ok(sealed: object) -> bool:
    """The hub cannot open a wrap; it checks only the bytes' length and that they start with a valid ephemeral point."""
    return type(sealed) is bytes and SEALED_MIN <= len(sealed) <= SEALED_MAX and S.valid_public_key(sealed[: S.PUB_LEN])


def valid_org_secrets(x: object) -> bool:
    """The ``secrets`` member of ``hub.org_secrets``: sorted by name, each ``{id, name, version, expires_ms, wxk_version,
    sealed}`` and nothing else. ``sealed`` is base64url of the wrap."""
    from .canon import CanonError, unb64u

    if type(x) is not list or len(x) > SECRETS_MAX:
        return False
    names = []
    for e in x:
        if type(e) is not dict or set(e) != {"id", "name", "version", "expires_ms", "wxk_version", "sealed"}:
            return False
        if type(e["id"]) is not str or _HEX16.fullmatch(e["id"]) is None:
            return False
        if type(e["name"]) is not str or NAME.fullmatch(e["name"]) is None:
            return False
        for k, bits in (("version", 32), ("wxk_version", 32), ("expires_ms", 64)):
            v = e[k]
            if type(v) is not int or not (1 if k != "expires_ms" else 0) <= v < 2**bits:
                return False
        try:
            if not sealed_shape_ok(unb64u(e["sealed"])):
                return False
        except CanonError:
            return False
        names.append(e["name"])
    return names == sorted(set(names))
