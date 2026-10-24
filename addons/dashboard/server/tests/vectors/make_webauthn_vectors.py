"""Writes tests/vectors/webauthn_*.json (host spec §0.8.6 item 10.6, #1198 R1-2).

Run from addons/dashboard/server: `uv run python tests/vectors/make_webauthn_vectors.py`. Independent of
`orch_dashboard.passkeys`: its own CBOR encoder and fixed keys, so a verifier bug does not shape the vectors. ES256
signatures are deterministic (RFC 6979), so a rerun gives the same files. Every case names the reason the verifier must
give (`"ok"` for a valid one).
"""

import base64
import hashlib
import json
import sys
from pathlib import Path

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature, encode_dss_signature

OUT = Path(__file__).parent
RP_ID = "orch.example.com"
ORIGIN = "https://orch.example.com"
P256_N = 0xFFFFFFFF00000000FFFFFFFFFFFFFFFFBCE6FAADA7179E84F3B9CAC2FC632551

ES_KEY = ec.derive_private_key(0x1198_0001_C0FFEE, ec.SECP256R1())
ES_OTHER = ec.derive_private_key(0x1198_0002_C0FFEE, ec.SECP256R1())
ED_KEY = Ed25519PrivateKey.from_private_bytes(bytes(range(32)))
ED_OTHER = Ed25519PrivateKey.from_private_bytes(bytes(range(1, 33)))

CHALLENGE = hashlib.sha256(b"r1-2 challenge").digest()
OTHER_CHALLENGE = hashlib.sha256(b"r1-2 other challenge").digest()
CRED_ES = hashlib.sha256(b"r1-2 credential es256").digest()
CRED_ED = hashlib.sha256(b"r1-2 credential eddsa").digest()[:20]
AAGUID = bytes(16)
UP, UV, BE, BS, AT, ED = 0x01, 0x04, 0x08, 0x10, 0x40, 0x80


def b64u(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


# --- a tiny CBOR encoder (shortest form, definite lengths) ---------------------------------------------------------


def head(major: int, n: int) -> bytes:
    if n < 24:
        return bytes([major << 5 | n])
    for info, size in ((24, 1), (25, 2), (26, 4), (27, 8)):
        if n < 1 << (8 * size):
            return bytes([major << 5 | info]) + n.to_bytes(size, "big")
    raise ValueError(n)


def cbor(v) -> bytes:
    if v is True:
        return b"\xf5"
    if v is False:
        return b"\xf4"
    if isinstance(v, int):
        return head(0, v) if v >= 0 else head(1, -1 - v)
    if isinstance(v, bytes):
        return head(2, len(v)) + v
    if isinstance(v, str):
        e = v.encode()
        return head(3, len(e)) + e
    if isinstance(v, list):
        return head(4, len(v)) + b"".join(cbor(x) for x in v)
    if isinstance(v, dict):
        return head(5, len(v)) + b"".join(cbor(k) + cbor(x) for k, x in v.items())
    raise TypeError(v)


def cbor_map_raw(pairs: list[tuple[bytes, bytes]]) -> bytes:
    """A map from already encoded keys and values (for duplicate keys and other malformed maps)."""
    return head(5, len(pairs)) + b"".join(k + v for k, v in pairs)


# --- keys ----------------------------------------------------------------------------------------------------------


def es_xy(key) -> tuple[bytes, bytes]:
    raw = key.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
    return raw[1:33], raw[33:]


def cose_es(key=ES_KEY) -> dict:
    x, y = es_xy(key)
    return {1: 2, 3: -7, -1: 1, -2: x, -3: y}


def cose_ed(key=ED_KEY) -> dict:
    raw = key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    return {1: 1, 3: -8, -1: 6, -2: raw}


def sign_es(key, msg: bytes) -> bytes:
    return key.sign(msg, ec.ECDSA(hashes.SHA256(), deterministic_signing=True))


def sign_ed(key, msg: bytes) -> bytes:
    return key.sign(msg)


# --- pieces --------------------------------------------------------------------------------------------------------


def client_data(kind: str, challenge: bytes = CHALLENGE, origin: str = ORIGIN, **extra) -> bytes:
    o = {"type": kind, "challenge": b64u(challenge), "origin": origin, "crossOrigin": False}
    o.update(extra)
    return json.dumps(o, separators=(",", ":")).encode()


def auth_head(flags: int, count: int = 0, rp_id: str = RP_ID) -> bytes:
    return hashlib.sha256(rp_id.encode()).digest() + bytes([flags]) + count.to_bytes(4, "big")


def attested(cred: bytes, cose: bytes) -> bytes:
    return AAGUID + len(cred).to_bytes(2, "big") + cred + cose


def att_object(auth_data: bytes, fmt="none", stmt=None, extra=None) -> bytes:
    o = {"fmt": fmt, "attStmt": {} if stmt is None else stmt, "authData": auth_data}
    if extra:
        o.update(extra)
    return cbor(o)


# --- registration --------------------------------------------------------------------------------------------------


def reg_case(name, expect, *, cred=CRED_ES, cose=None, flags=UP | UV | AT, cd=None, att=None, auth_data=None, **kw):
    cose_bytes = cbor(cose_es()) if cose is None else cose
    if auth_data is None:
        auth_data = auth_head(flags) + attested(cred, cose_bytes)
    case = {
        "name": name,
        "credential_id": b64u(kw.get("reported_id", cred)),
        "client_data_json": b64u(cd if cd is not None else client_data("webauthn.create")),
        "attestation_object": b64u(att if att is not None else att_object(auth_data)),
        "challenge": b64u(CHALLENGE),
        "expect": expect,
    }
    if expect == "ok":
        case["public_key"] = b64u(cose_bytes)
        case["alg"] = cbor_alg(cose_bytes)
    return case


def cbor_alg(cose_bytes: bytes) -> int:
    return -7 if cose_bytes == cbor(cose_es()) else -8


def registration_cases():
    es, ed = cbor(cose_es()), cbor(cose_ed())
    good_ad = auth_head(UP | UV | AT) + attested(CRED_ES, es)
    chrome_cd = client_data(
        "webauthn.create",
        other_keys_can_be_added_here="do not compare clientDataJSON against a template. See https://goo.gl/yabPex",
    )
    x, y = es_xy(ES_KEY)
    off_curve = {1: 2, 3: -7, -1: 1, -2: x, -3: bytes(31) + b"\x01"}
    return [
        reg_case("es256 none", "ok"),
        reg_case("eddsa none", "ok", cred=CRED_ED, cose=ed),
        reg_case("chrome extra clientData key", "ok", cd=chrome_cd),
        reg_case("synced passkey BE+BS", "ok", flags=UP | UV | AT | BE | BS),
        reg_case(
            "extension output credProtect",
            "ok",
            auth_data=auth_head(UP | UV | AT | ED) + attested(CRED_ES, es) + cbor({"credProtect": 2}),
        ),
        reg_case("type webauthn.get", "client_data_type", cd=client_data("webauthn.get")),
        reg_case("other challenge", "challenge", cd=client_data("webauthn.create", OTHER_CHALLENGE)),
        reg_case(
            "challenge with padding",
            "challenge",
            cd=json.dumps({"type": "webauthn.create", "challenge": b64u(CHALLENGE) + "=", "origin": ORIGIN}).encode(),
        ),
        reg_case("origin http", "origin", cd=client_data("webauthn.create", origin="http://orch.example.com")),
        reg_case("origin with port", "origin", cd=client_data("webauthn.create", origin=ORIGIN + ":443")),
        reg_case("origin look-alike", "origin", cd=client_data("webauthn.create", origin="https://orch-example.com")),
        reg_case("origin subdomain", "origin", cd=client_data("webauthn.create", origin="https://a.orch.example.com")),
        reg_case("crossOrigin true", "cross_origin", cd=client_data("webauthn.create", crossOrigin=True)),
        reg_case("topOrigin present", "top_origin", cd=client_data("webauthn.create", topOrigin=ORIGIN)),
        reg_case(
            "tokenBinding present",
            "token_binding",
            cd=client_data("webauthn.create", tokenBinding={"status": "present", "id": "AAAA"}),
        ),
        reg_case(
            "duplicate clientData key",
            "client_data_duplicate_key",
            cd=client_data("webauthn.create")[:-1] + b',"origin":"https://evil.example"}',
        ),
        reg_case("clientData not json", "client_data_json", cd=b"\xff\xfe"),
        reg_case("clientData array", "client_data_json", cd=b"[]"),
        reg_case(
            "rpIdHash of another name",
            "rp_id_hash",
            auth_data=auth_head(UP | UV | AT, rp_id="orch-example.com") + attested(CRED_ES, es),
        ),
        reg_case("no user presence", "user_presence", flags=UV | AT),
        reg_case("no user verification", "user_verification", flags=UP | AT),
        reg_case("BS without BE", "backup_state", flags=UP | UV | AT | BS),
        reg_case("no attested data flag", "attested_data_missing", flags=UP | UV),
        reg_case("fmt packed", "attestation_format", att=att_object(good_ad, fmt="packed", stmt={"alg": -7})),
        reg_case("none with statement", "attestation_format", att=att_object(good_ad, stmt={"x": 1})),
        reg_case("extra top-level key", "attestation_shape", att=att_object(good_ad, extra={"ext": 1})),
        reg_case("reported id differs", "credential_id_mismatch", reported_id=CRED_ED + bytes(12)),
        reg_case("credential id too short", "credential_id", cred=bytes(15), reported_id=bytes(15)),
        reg_case("rs256 key", "cose_alg", cose=cbor({1: 3, 3: -257, -1: b"\x01" * 256, -2: b"\x01\x00\x01"})),
        reg_case("es256 off curve", "cose_es256_point", cose=cbor(off_curve)),
        reg_case("es256 extra label", "cose_es256_labels", cose=cbor({**cose_es(), 2: b"kid"})),
        reg_case("es256 kty okp", "cose_es256_labels", cose=cbor({**cose_es(), 1: 1})),
        reg_case("ed448", "cose_eddsa_labels", cose=cbor({1: 1, 3: -8, -1: 7, -2: bytes(57)})),
        reg_case("eddsa small order", "cose_eddsa_small_order", cose=cbor({1: 1, 3: -8, -1: 6, -2: bytes(32)})),
        reg_case("alg as true", "cose_alg", cose=cbor({1: 2, 3: True, -1: 1, -2: x, -3: y})),
        reg_case("trailing byte after key", "auth_data_trailing", auth_data=good_ad + b"\x00"),
        reg_case(
            "ED flag without extensions",
            "cbor_truncated",
            auth_data=auth_head(UP | UV | AT | ED) + attested(CRED_ES, es),
        ),
        reg_case(
            "non-shortest cbor length",
            "cbor_not_shortest",
            att=b"\xb8\x03" + att_object(good_ad)[1:],
        ),
        reg_case("indefinite map", "cbor_indefinite", att=b"\xbf" + att_object(good_ad)[1:] + b"\xff"),
        reg_case("tagged", "cbor_tag", att=b"\xd8\x18" + att_object(good_ad)),
        reg_case("float", "cbor_simple", att=cbor_map_raw([(cbor("fmt"), b"\xf9\x00\x00")])),
        reg_case(
            "duplicate cbor key",
            "cbor_duplicate_key",
            att=cbor_map_raw(
                [(cbor("fmt"), cbor("none")), (cbor("fmt"), cbor("none")), (cbor("authData"), cbor(good_ad))]
            ),
        ),
        reg_case("trailing after attestation", "cbor_trailing", att=att_object(good_ad) + b"\x00"),
    ]


# --- assertion -----------------------------------------------------------------------------------------------------


def asr_case(
    name, expect, *, key="es", flags=UP | UV, cd=None, ad=None, sig=None, sign_key=None, challenge=CHALLENGE, count=7
):
    cose = cbor(cose_es()) if key == "es" else cbor(cose_ed())
    cred = CRED_ES if key == "es" else CRED_ED
    ad = auth_head(flags, count) if ad is None else ad
    cd = client_data("webauthn.get", challenge) if cd is None else cd
    msg = ad + hashlib.sha256(cd).digest()
    if sig is None:
        k = sign_key or (ES_KEY if key == "es" else ED_KEY)
        sig = sign_es(k, msg) if isinstance(k, ec.EllipticCurvePrivateKey) else sign_ed(k, msg)
    elif callable(sig):
        sig = sig(msg)
    return {
        "name": name,
        "credential_id": b64u(cred),
        "public_key": b64u(cose),
        "client_data_json": b64u(cd),
        "authenticator_data": b64u(ad),
        "signature": b64u(sig),
        "challenge": b64u(challenge),
        "expect": expect,
        **({"sign_count": count} if expect == "ok" else {}),
    }


def high_s(msg: bytes) -> bytes:
    r, s = decode_dss_signature(sign_es(ES_KEY, msg))
    return encode_dss_signature(r, s if s > P256_N // 2 else P256_N - s)


def ber_sig(msg: bytes) -> bytes:
    der = sign_es(ES_KEY, msg)
    # Re-encode the SEQUENCE length in long form (0x81 nn): valid BER, not DER.
    return bytes([0x30, 0x81, der[1]]) + der[2:]


ED25519_L = 2**252 + 27742317777372353535851937790883648493


def ed_s_plus_l(msg: bytes) -> bytes:
    """A valid Ed25519 signature with S replaced by S + L: the same S modulo L, but not canonical."""
    sig = sign_ed(ED_KEY, msg)
    return sig[:32] + (int.from_bytes(sig[32:], "little") + ED25519_L).to_bytes(32, "little")


def assertion_cases():
    return [
        asr_case("es256", "ok"),
        asr_case("eddsa", "ok", key="ed"),
        asr_case("es256 high-s accepted", "ok", sig=high_s),
        asr_case("synced passkey count 0", "ok", flags=UP | UV | BE | BS, count=0),
        asr_case("extension output", "ok", ad=auth_head(UP | UV | ED, 7) + cbor({"credProtect": 2})),
        asr_case(
            "chrome extra clientData key",
            "ok",
            cd=client_data("webauthn.get", other_keys_can_be_added_here="do not compare clientDataJSON"),
        ),
        asr_case("type webauthn.create", "client_data_type", cd=client_data("webauthn.create")),
        asr_case("other challenge", "challenge", cd=client_data("webauthn.get", OTHER_CHALLENGE)),
        asr_case("origin look-alike", "origin", cd=client_data("webauthn.get", origin="https://orch.example.co")),
        asr_case("rpIdHash of phishing name", "rp_id_hash", ad=auth_head(UP | UV, 7, rp_id="orch.example.co")),
        asr_case("no user presence", "user_presence", flags=UV),
        asr_case("no user verification", "user_verification", flags=UP),
        asr_case("BS without BE", "backup_state", flags=UP | UV | BS),
        asr_case("AT in assertion", "attested_data_in_assertion", flags=UP | UV | AT),
        asr_case("trailing byte", "auth_data_trailing", ad=auth_head(UP | UV, 7) + b"\x00"),
        asr_case("short auth data", "auth_data_short", ad=auth_head(UP | UV, 7)[:36]),
        asr_case("es256 wrong key", "signature", sign_key=ES_OTHER),
        asr_case("eddsa wrong key", "signature", key="ed", sign_key=ED_OTHER),
        asr_case("es256 ber signature", "signature_der", sig=ber_sig),
        asr_case("es256 raw r||s", "signature_der", sig=lambda m: bytes(64)),
        asr_case("eddsa short signature", "signature_size", key="ed", sig=lambda m: sign_ed(ED_KEY, m)[:63]),
        asr_case("empty signature", "signature_size", sig=b""),
        asr_case(
            "eddsa flipped bit",
            "signature",
            key="ed",
            sig=lambda m: bytes([sign_ed(ED_KEY, m)[0] ^ 1]) + sign_ed(ED_KEY, m)[1:],
        ),
        asr_case(
            "eddsa non-canonical S",
            "signature",
            key="ed",
            sig=ed_s_plus_l,
        ),
    ]


# --- step-up -------------------------------------------------------------------------------------------------------

NONCE = hashlib.sha256(b"r1-2 step-up nonce").digest()
VIEW_DIGEST = "sha256:" + hashlib.sha256(b"r1-2 a view").hexdigest()
OTHER_VIEW = "sha256:" + hashlib.sha256(b"r1-2 another view").hexdigest()


def stepup(nonce: bytes, vd: str) -> bytes:
    return hashlib.sha256(b"orch/v2/dash-stepup|" + nonce + bytes.fromhex(vd[7:])).digest()


def stepup_vectors():
    good = stepup(NONCE, VIEW_DIGEST)
    return {
        "label": "orch/v2/dash-stepup|",
        "challenges": [
            {"nonce": b64u(NONCE), "view_digest": VIEW_DIGEST, "challenge": b64u(good)},
            {"nonce": b64u(NONCE), "view_digest": OTHER_VIEW, "challenge": b64u(stepup(NONCE, OTHER_VIEW))},
            {"nonce": b64u(bytes(32)), "view_digest": VIEW_DIGEST, "challenge": b64u(stepup(bytes(32), VIEW_DIGEST))},
        ],
        "bad_inputs": [
            {"nonce": b64u(bytes(31)), "view_digest": VIEW_DIGEST, "expect": "stepup_nonce"},
            {"nonce": b64u(NONCE), "view_digest": VIEW_DIGEST.upper(), "expect": "stepup_view_digest"},
            {"nonce": b64u(NONCE), "view_digest": VIEW_DIGEST[7:], "expect": "stepup_view_digest"},
            {"nonce": b64u(NONCE), "view_digest": VIEW_DIGEST + "00", "expect": "stepup_view_digest"},
        ],
        # Assertions made for the step-up of VIEW_DIGEST; each is checked against the challenge of `check_view`.
        "assertions": [
            {**asr_case("step-up for the shown view", "ok", challenge=good), "check_view": VIEW_DIGEST},
            {**asr_case("step-up replayed for another view", "challenge", challenge=good), "check_view": OTHER_VIEW},
            {
                **asr_case("login assertion used as step-up", "challenge", challenge=NONCE),
                "check_view": VIEW_DIGEST,
            },
        ],
        "nonce": b64u(NONCE),
    }


def main():
    common = {"rp_id": RP_ID, "origin": ORIGIN, "spec": "orch-v2-host.md §0.8.6 item 10.6"}
    files = {
        "webauthn_registration.json": {**common, "cases": registration_cases()},
        "webauthn_assertion.json": {**common, "cases": assertion_cases()},
        "webauthn_stepup.json": {**common, **stepup_vectors()},
    }
    for name, body in files.items():
        (OUT / name).write_text(json.dumps(body, indent=1) + "\n")
        print(name, len(body.get("cases") or body.get("assertions")), file=sys.stderr)


if __name__ == "__main__":
    main()
