"""WebAuthn verification for the remote dashboard (host spec §0.8.6 item 10.6, #1198 R1-2).

Verification functions only: no routes, no store, no challenge bookkeeping (R1-5 owns those). An own small verifier on
`cryptography` instead of the `webauthn` package: it accepts exactly what the remote dashboard asks browsers for
(attestation "none", user verification required, ES256 or EdDSA) and nothing else, so the internet-facing pre-login
path parses a strict CBOR subset and no attestation format. Every failure raises `PasskeyError` (catalog code
`passkey.invalid`); its `reason` is for the log, never for an unauthenticated client.
"""

import base64
import binascii
import hashlib
import hmac
import json
import re
import secrets
from dataclasses import dataclass

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature, encode_dss_signature

ALG_ES256 = -7
ALG_EDDSA = -8
ALGORITHMS = (ALG_ES256, ALG_EDDSA)

CHALLENGE_BYTES = 32
STEPUP_LABEL = b"orch/v2/dash-stepup|"

MAX_CLIENT_DATA = 4096
MAX_ATTESTATION_OBJECT = 8192
MAX_AUTH_DATA = 4096
MAX_SIGNATURE = 128
MIN_CREDENTIAL_ID = 16
MAX_CREDENTIAL_ID = 1023
MAX_USER_HANDLE = 64
CBOR_MAX_DEPTH = 8

FLAG_UP = 0x01
FLAG_UV = 0x04
FLAG_BE = 0x08
FLAG_BS = 0x10
FLAG_AT = 0x40
FLAG_ED = 0x80

# The Ed25519 encodings of points of small order (libsodium's blocklist, including non-canonical spellings of them).
# A key of small order lets anyone forge signatures for it with some probability under cofactorless verification.
ED25519_SMALL_ORDER = frozenset(
    bytes.fromhex(h)
    for h in (
        "0000000000000000000000000000000000000000000000000000000000000000",
        "0000000000000000000000000000000000000000000000000000000000000080",
        "0100000000000000000000000000000000000000000000000000000000000000",
        "0100000000000000000000000000000000000000000000000000000000000080",
        "ecffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff7f",
        "ecffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff",
        "26e8958fc2b227b045c3f489f2ef98f0d5dfac05d3c63339b13802886d53fc05",
        "26e8958fc2b227b045c3f489f2ef98f0d5dfac05d3c63339b13802886d53fc85",
        "c7176a703d4dd84fba3c0b760d10670f2a2053fa2c39ccc64ec7fd7792ac037a",
        "c7176a703d4dd84fba3c0b760d10670f2a2053fa2c39ccc64ec7fd7792ac03fa",
        "edffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff7f",
        "edffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff",
        "eeffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff7f",
        "eeffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff",
    )
)

_LABEL = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?")
_B64U = re.compile(r"[A-Za-z0-9_-]*")
_VIEW_DIGEST = re.compile(r"sha256:[0-9a-f]{64}")


class PasskeyError(Exception):
    code = "passkey.invalid"

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class Registration:
    credential_id: bytes
    public_key: bytes  # the COSE key exactly as the authenticator sent it; `verify_assertion` takes it back
    alg: int
    sign_count: int
    aaguid: bytes
    backup_eligible: bool
    backed_up: bool


@dataclass(frozen=True)
class Assertion:
    credential_id: bytes
    sign_count: int
    backup_eligible: bool
    backed_up: bool


def new_challenge() -> bytes:
    return secrets.token_bytes(CHALLENGE_BYTES)


def stepup_challenge(nonce: bytes, view_digest: str) -> bytes:
    """The WebAuthn challenge of a step-up: SHA-256(label || nonce || the 32 raw bytes of `view_digest`)."""
    if not isinstance(nonce, bytes) or len(nonce) != CHALLENGE_BYTES:
        raise PasskeyError("stepup_nonce")
    if not isinstance(view_digest, str) or not _VIEW_DIGEST.fullmatch(view_digest):
        raise PasskeyError("stepup_view_digest")
    return hashlib.sha256(STEPUP_LABEL + nonce + bytes.fromhex(view_digest[7:])).digest()


def b64u_decode(text: str, max_len: int) -> bytes:
    """Strict base64url: URL-safe alphabet, no padding, zero trailing bits, at most `max_len` decoded bytes."""
    if not isinstance(text, str) or not _B64U.fullmatch(text) or len(text) % 4 == 1:
        raise PasskeyError("b64u")
    if len(text) > (max_len * 4 + 2) // 3:
        raise PasskeyError("too_long")
    try:
        raw = base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))
    except (binascii.Error, ValueError):
        raise PasskeyError("b64u") from None
    if b64u_encode(raw) != text:
        raise PasskeyError("b64u")
    return raw


def b64u_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def check_rp(rp_id: str, origin: str) -> None:
    """The RP ID is a lower-case DNS name with two labels or more, not an IP; the origin is exactly https:// + RP ID."""
    if not isinstance(rp_id, str) or len(rp_id) > 253:
        raise PasskeyError("rp_id")
    labels = rp_id.split(".")
    if len(labels) < 2 or not all(_LABEL.fullmatch(label) for label in labels) or labels[-1].isdigit():
        raise PasskeyError("rp_id")
    if origin != "https://" + rp_id:
        raise PasskeyError("origin_config")


# --- CBOR (a strict subset) ---------------------------------------------------------------------------------------


def _cbor_item(data: bytes, pos: int, depth: int):
    if depth > CBOR_MAX_DEPTH:
        raise PasskeyError("cbor_depth")
    if pos >= len(data):
        raise PasskeyError("cbor_truncated")
    head = data[pos]
    major, info = head >> 5, head & 0x1F
    pos += 1
    if major == 7:
        if info == 20:
            return False, pos
        if info == 21:
            return True, pos
        raise PasskeyError("cbor_simple")
    if major == 6:
        raise PasskeyError("cbor_tag")
    if info < 24:
        arg = info
    elif info in (24, 25, 26, 27):
        size = 1 << (info - 24)
        if pos + size > len(data):
            raise PasskeyError("cbor_truncated")
        arg = int.from_bytes(data[pos : pos + size], "big")
        pos += size
        if arg < (24 if size == 1 else 1 << (4 * size)):
            raise PasskeyError("cbor_not_shortest")
    else:
        raise PasskeyError("cbor_indefinite")
    if major == 0:
        return arg, pos
    if major == 1:
        return -1 - arg, pos
    if major in (2, 3):
        if arg > len(data) - pos:
            raise PasskeyError("cbor_truncated")
        raw = data[pos : pos + arg]
        pos += arg
        if major == 2:
            return raw, pos
        try:
            return raw.decode("utf-8"), pos
        except UnicodeDecodeError:
            raise PasskeyError("cbor_utf8") from None
    if arg > len(data) - pos:  # every element takes at least one byte
        raise PasskeyError("cbor_truncated")
    if major == 4:
        items = []
        for _ in range(arg):
            item, pos = _cbor_item(data, pos, depth + 1)
            items.append(item)
        return items, pos
    out = {}
    for _ in range(arg):
        key, pos = _cbor_item(data, pos, depth + 1)
        if isinstance(key, bool) or not isinstance(key, int | str):
            raise PasskeyError("cbor_key_type")
        if key in out:
            raise PasskeyError("cbor_duplicate_key")
        out[key], pos = _cbor_item(data, pos, depth + 1)
    return out, pos


def cbor_decode_prefix(data: bytes, pos: int = 0):
    """One item from `data` at `pos`: (item, end)."""
    return _cbor_item(data, pos, 1)


def cbor_decode(data: bytes):
    item, end = cbor_decode_prefix(data)
    if end != len(data):
        raise PasskeyError("cbor_trailing")
    return item


# --- COSE keys ----------------------------------------------------------------------------------------------------


def _same(a, b) -> bool:
    """Exact equality where True never equals 1 (CBOR `true` is not the integer 1)."""
    return type(a) is type(b) and a == b


def _check_cose(key) -> tuple[int, object]:
    if not isinstance(key, dict):
        raise PasskeyError("cose_type")
    alg = key.get(3)
    if _same(alg, ALG_ES256):
        if set(key) != {1, 3, -1, -2, -3} or not _same(key[1], 2) or not _same(key[-1], 1):
            raise PasskeyError("cose_es256_labels")
        x, y = key[-2], key[-3]
        if not (isinstance(x, bytes) and isinstance(y, bytes) and len(x) == 32 and len(y) == 32):
            raise PasskeyError("cose_es256_point")
        try:
            pub = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), b"\x04" + x + y)
        except ValueError:
            raise PasskeyError("cose_es256_point") from None
        return ALG_ES256, pub
    if _same(alg, ALG_EDDSA):
        if set(key) != {1, 3, -1, -2} or not _same(key[1], 1) or not _same(key[-1], 6):
            raise PasskeyError("cose_eddsa_labels")
        x = key[-2]
        if not isinstance(x, bytes) or len(x) != 32:
            raise PasskeyError("cose_eddsa_point")
        if x in ED25519_SMALL_ORDER:
            raise PasskeyError("cose_eddsa_small_order")
        try:
            pub = Ed25519PublicKey.from_public_bytes(x)
        except ValueError:
            raise PasskeyError("cose_eddsa_point") from None
        return ALG_EDDSA, pub
    raise PasskeyError("cose_alg")


def parse_public_key(cose: bytes) -> tuple[int, object]:
    """A stored COSE key back to (alg, key object), with every check of registration."""
    if not isinstance(cose, bytes) or len(cose) > 256:
        raise PasskeyError("cose_type")
    return _check_cose(cbor_decode(cose))


# --- clientDataJSON and authenticatorData ---------------------------------------------------------------------------


def _no_duplicates(pairs):
    out = {}
    for k, v in pairs:
        if k in out:
            raise PasskeyError("client_data_duplicate_key")
        out[k] = v
    return out


def _check_client_data(raw: bytes, kind: str, challenge: bytes, origin: str) -> None:
    if not isinstance(raw, bytes) or len(raw) > MAX_CLIENT_DATA:
        raise PasskeyError("client_data_size")
    try:
        cd = json.loads(raw.decode("utf-8"), object_pairs_hook=_no_duplicates)
    except PasskeyError:
        raise
    except (UnicodeDecodeError, ValueError, RecursionError):
        raise PasskeyError("client_data_json") from None
    if not isinstance(cd, dict):
        raise PasskeyError("client_data_json")
    if cd.get("type") != kind:
        raise PasskeyError("client_data_type")
    sent = cd.get("challenge")
    if not isinstance(sent, str):
        raise PasskeyError("challenge")
    try:
        sent_raw = b64u_decode(sent, 64)
    except PasskeyError:
        raise PasskeyError("challenge") from None
    if not (isinstance(challenge, bytes) and len(challenge) == CHALLENGE_BYTES):
        raise PasskeyError("expected_challenge")
    if not hmac.compare_digest(sent_raw, challenge):
        raise PasskeyError("challenge")
    if not isinstance(cd.get("origin"), str) or cd["origin"] != origin:
        raise PasskeyError("origin")
    if "crossOrigin" in cd and cd["crossOrigin"] is not False:
        raise PasskeyError("cross_origin")
    if "topOrigin" in cd:
        raise PasskeyError("top_origin")
    tb = cd.get("tokenBinding")
    if tb is not None and (not isinstance(tb, dict) or tb.get("status") == "present"):
        raise PasskeyError("token_binding")


def _check_auth_data_head(data: bytes, rp_id: str) -> tuple[int, int]:
    if len(data) < 37:
        raise PasskeyError("auth_data_short")
    if not hmac.compare_digest(data[:32], hashlib.sha256(rp_id.encode("ascii")).digest()):
        raise PasskeyError("rp_id_hash")
    flags = data[32]
    if not flags & FLAG_UP:
        raise PasskeyError("user_presence")
    if not flags & FLAG_UV:
        raise PasskeyError("user_verification")
    if flags & FLAG_BS and not flags & FLAG_BE:
        raise PasskeyError("backup_state")
    return flags, int.from_bytes(data[33:37], "big")


def _check_extensions(data: bytes, pos: int, flags: int) -> None:
    if flags & FLAG_ED:
        ext, pos = cbor_decode_prefix(data, pos)
        if not isinstance(ext, dict):
            raise PasskeyError("extensions")
    if pos != len(data):
        raise PasskeyError("auth_data_trailing")


# --- the two ceremonies ---------------------------------------------------------------------------------------------


def verify_registration(
    *,
    credential_id: bytes,
    client_data_json: bytes,
    attestation_object: bytes,
    challenge: bytes,
    rp_id: str,
    origin: str,
) -> Registration:
    """A `navigator.credentials.create` response, `credential_id` being the raw id the browser reported."""
    check_rp(rp_id, origin)
    if not isinstance(credential_id, bytes) or not MIN_CREDENTIAL_ID <= len(credential_id) <= MAX_CREDENTIAL_ID:
        raise PasskeyError("credential_id")
    _check_client_data(client_data_json, "webauthn.create", challenge, origin)
    if not isinstance(attestation_object, bytes) or len(attestation_object) > MAX_ATTESTATION_OBJECT:
        raise PasskeyError("attestation_size")
    att = cbor_decode(attestation_object)
    if not isinstance(att, dict) or set(att) != {"fmt", "attStmt", "authData"}:
        raise PasskeyError("attestation_shape")
    if att["fmt"] != "none" or att["attStmt"] != {}:
        raise PasskeyError("attestation_format")
    data = att["authData"]
    if not isinstance(data, bytes) or len(data) > MAX_AUTH_DATA:
        raise PasskeyError("auth_data_type")
    flags, count = _check_auth_data_head(data, rp_id)
    if not flags & FLAG_AT:
        raise PasskeyError("attested_data_missing")
    if len(data) < 55:
        raise PasskeyError("auth_data_short")
    aaguid = data[37:53]
    n = int.from_bytes(data[53:55], "big")
    if not MIN_CREDENTIAL_ID <= n <= MAX_CREDENTIAL_ID or 55 + n > len(data):
        raise PasskeyError("credential_id")
    cred = data[55 : 55 + n]
    if not hmac.compare_digest(cred, credential_id):
        raise PasskeyError("credential_id_mismatch")
    key, end = cbor_decode_prefix(data, 55 + n)
    alg, _ = _check_cose(key)
    _check_extensions(data, end, flags)
    return Registration(
        credential_id=cred,
        public_key=data[55 + n : end],
        alg=alg,
        sign_count=count,
        aaguid=aaguid,
        backup_eligible=bool(flags & FLAG_BE),
        backed_up=bool(flags & FLAG_BS),
    )


def verify_assertion(
    *,
    credential_id: bytes,
    client_data_json: bytes,
    authenticator_data: bytes,
    signature: bytes,
    public_key: bytes,
    challenge: bytes,
    rp_id: str,
    origin: str,
) -> Assertion:
    """A `navigator.credentials.get` response against the stored COSE `public_key` of `credential_id`.

    For a step-up, `challenge` is `stepup_challenge(nonce, view_digest)`. Finding the credential, its user handle and
    the single use of the challenge are the caller's (R1-5, R1-6).
    """
    check_rp(rp_id, origin)
    if not isinstance(credential_id, bytes) or not MIN_CREDENTIAL_ID <= len(credential_id) <= MAX_CREDENTIAL_ID:
        raise PasskeyError("credential_id")
    alg, pub = parse_public_key(public_key)
    _check_client_data(client_data_json, "webauthn.get", challenge, origin)
    data = authenticator_data
    if not isinstance(data, bytes) or len(data) > MAX_AUTH_DATA:
        raise PasskeyError("auth_data_type")
    flags, count = _check_auth_data_head(data, rp_id)
    if flags & FLAG_AT:
        raise PasskeyError("attested_data_in_assertion")
    _check_extensions(data, 37, flags)
    if not isinstance(signature, bytes) or not 0 < len(signature) <= MAX_SIGNATURE:
        raise PasskeyError("signature_size")
    signed = data + hashlib.sha256(client_data_json).digest()
    try:
        if alg == ALG_ES256:
            try:
                r, s = decode_dss_signature(signature)
            except ValueError:
                raise PasskeyError("signature_der") from None
            if encode_dss_signature(r, s) != signature:
                raise PasskeyError("signature_der")
            pub.verify(signature, signed, ec.ECDSA(hashes.SHA256()))
        else:
            if len(signature) != 64:
                raise PasskeyError("signature_size")
            pub.verify(signature, signed)
    except InvalidSignature:
        raise PasskeyError("signature") from None
    return Assertion(
        credential_id=credential_id,
        sign_count=count,
        backup_eligible=bool(flags & FLAG_BE),
        backed_up=bool(flags & FLAG_BS),
    )
